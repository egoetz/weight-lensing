from argparse import Namespace
from datasets import Dataset
from collections import defaultdict
from itertools import islice
from torch.utils.data import DataLoader
from tqdm.auto import tqdm
from white_box import record_residual_stream, LogitStats, ResidualStats, TunedLens
from white_box.utils import (
    maybe_shift_preds,
    send_to_device,
)
import torch as th
import torch.distributed as dist


@th.autocast("cuda", enabled=th.cuda.is_available())
@th.no_grad()
def eval_loop(
    args: Namespace,
    model: th.nn.Module,
    data: Dataset,
    lens: TunedLens,
):
    local_rank = dist.get_rank() if dist.is_initialized() else 0
    dl = DataLoader(
        data.shuffle(seed=args.seed),  # type: ignore[arg-type],
        batch_size=args.per_gpu_batch_size,
    )
    lens.eval()

    # Running mean & covariance of the hidden states
    first_token_stats = ResidualStats()
    delta_stats = ResidualStats()
    stream_stats = ResidualStats()
    logit_stats = LogitStats()

    if args.limit:
        dl = islice(dl, args.limit)
        total = args.limit
    else:
        total = len(dl)

    root_dir = args.output or args.lens / "eval"
    output_dir = root_dir / f"rank_{local_rank}"
    output_dir.mkdir(exist_ok=True, parents=True)

    pbar = tqdm(dl, desc="Evaluating", position=local_rank, total=total)
    for batch in pbar:
        batch = send_to_device(batch, th.device(local_rank))
        with record_residual_stream(model) as stream:
            output = model(**batch)

        final_lps = output.logits.log_softmax(dim=-1)
        final_probs = final_lps.exp()
        labels = batch["input_ids"][:, 1:]

        # Do this sequentially to save VRAM
        batch_output = defaultdict(dict)
        for i, (name, h) in zip(range(len(lens)), stream.items()):
            lens_lps = lens(h, idx=i).log_softmax(dim=-1)

            baseline_lps = lens.to_logits(h).log_softmax(dim=-1)
            batch_output["baseline_ce"][name] = th.nn.functional.cross_entropy(
                maybe_shift_preds(baseline_lps, 1).flatten(0, 1),
                labels.flatten(),
                reduction="none",
            )
            batch_output["baseline_entropy"][name] = th.sum(
                -baseline_lps.exp() * baseline_lps, dim=-1
            )
            batch_output["lens_ce"][name] = th.nn.functional.cross_entropy(
                maybe_shift_preds(lens_lps, 1).flatten(0, 1),
                labels.flatten(),
                reduction="none",
            )
            batch_output["lens_entropy"][name] = th.sum(
                -lens_lps.exp() * lens_lps, dim=-1
            )
            batch_output["lens_kl"][name] = th.sum(
                final_probs * (final_lps - lens_lps), dim=-1
            )

        first_tokens = stream.map(lambda x: x[:, 0])
        rest = stream.map(lambda x: x[:, 1:])

        first_token_stats.update(first_tokens)
        delta_stats.update(rest.residuals())
        logit_stats.update(final_lps, assume_normalized=True)
        stream_stats.update(rest)

        batch_output["baseline_ce"]["final"] = th.nn.functional.cross_entropy(
            maybe_shift_preds(final_lps, 1).flatten(0, 1),
            labels.flatten(),
            reduction="none",
        )
        batch_output["baseline_entropy"]["final"] = th.sum(
            -final_probs * final_lps, dim=-1
        )
        th.save(batch_output, output_dir / f"batch_{pbar.n}.pt")

    pbar.close()
    th.save(first_token_stats, output_dir / "first_token_stats.pt")
    th.save(delta_stats, output_dir / "delta_stats.pt")
    th.save(stream_stats, output_dir / "stream_stats.pt")
    th.save(logit_stats, output_dir / "logit_stats.pt")