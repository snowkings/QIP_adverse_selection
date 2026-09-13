"""Evaluate QIP threshold crossings on one supplied 50 ms session grid."""
import argparse
import hashlib
import json
import platform
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

HORIZONS_MS = (100, 250, 500, 1000, 5000)
THRESHOLD = 0.125


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD; regular full-length RTH day")
    parser.add_argument("--output", type=Path, default=Path("results"))
    args = parser.parse_args()
    date = datetime.strptime(args.date, "%Y-%m-%d").date()
    ny = ZoneInfo("America/New_York")
    start = datetime.combine(date, time(9, 30), ny).astimezone(timezone.utc)
    end = datetime.combine(date, time(16, 0), ny).astimezone(timezone.utc)
    columns = ["DateTime", "qip", "mid", "best_bid", "best_ask", "is_synthetic_bar"]
    q = pl.read_parquet(args.input, columns=columns)
    if q.schema["DateTime"].time_zone is not None:
        raise ValueError("This evaluation expects the project's UTC-naive QIP timestamps.")
    q = q.with_columns(pl.col("DateTime").dt.replace_time_zone("UTC"))
    if not q["DateTime"].is_sorted() or q["DateTime"].n_unique() != q.height:
        raise ValueError("Source timestamps must be sorted and unique.")
    prices = ["qip", "mid", "best_bid", "best_ask"]
    q = q.with_columns(
        (pl.all_horizontal([pl.col(c).is_finite() & (pl.col(c) > 0) for c in prices])
         & (pl.col("best_ask") >= pl.col("best_bid"))).fill_null(False).alias("valid_quote"),
        (pl.col("qip") - pl.col("mid")).alias("signal"),
    ).with_columns(
        pl.col("signal").sign().cast(pl.Int8).alias("signal_dir"),
        (pl.col("valid_quote") & (pl.col("signal").abs() >= THRESHOLD)).alias("passed"),
    )
    # Build crossings before the RTH filter, retaining the preceding grid state.
    q = q.with_columns(
        (pl.col("passed") & (
            ~pl.col("passed").shift(1).fill_null(False)
            | (pl.col("signal_dir") != pl.col("signal_dir").shift(1).fill_null(0))
        )).alias("trigger")
    )
    rth = q.filter((pl.col("DateTime") >= start) & (pl.col("DateTime") < end))
    if rth.is_empty():
        raise ValueError("No RTH data for the requested date.")
    if (rth.height != 468000 or rth["DateTime"][0] != start
            or rth["DateTime"][-1] != end - timedelta(milliseconds=50)
            or rth.select((pl.col("DateTime").diff().dt.total_nanoseconds().drop_nulls() != 50_000_000).sum()).item()):
        raise ValueError("Expected a complete full-length RTH grid at exactly 50 ms.")
    if not rth["valid_quote"].all():
        raise ValueError("Invalid price or bid/ask values inside RTH.")
    if rth.filter(pl.col("mid") != (pl.col("best_bid") + pl.col("best_ask")) / 2).height:
        raise ValueError("RTH midpoint differs from the bid/ask average.")
    events = rth.filter(pl.col("trigger")).select(columns + ["signal", "signal_dir"]).with_row_index("event_id")
    if events.is_empty():
        raise ValueError("No signal events in the requested session.")
    summary = []
    for h in HORIZONS_MS:
        target, future, fmid = f"target_{h}ms", f"future_{h}ms", f"mid_fwd_{h}ms"
        valid, delay, markout, outcome = (f"valid_{h}ms", f"delay_{h}ms", f"markout_{h}ms", f"outcome_{h}ms")
        # Only within-session quotes are eligible; no overnight/after-close lookup.
        future_quotes = rth.select(pl.col("DateTime").alias(future), pl.col("mid").alias(fmid))
        events = events.with_columns((pl.col("DateTime") + pl.duration(milliseconds=h, time_unit="ns")).alias(target))
        events = events.join_asof(future_quotes, left_on=target, right_on=future,
                                 strategy="forward", tolerance=timedelta(milliseconds=50))
        events = events.with_columns(
            pl.col(future).is_not_null().alias(valid),
            ((pl.col(future) - pl.col(target)).dt.total_nanoseconds() / 1_000_000).alias(delay),
            (pl.col("signal_dir") * (pl.col(fmid) - pl.col("mid"))).alias(markout),
        ).with_columns(
            pl.when(~pl.col(valid)).then(pl.lit("unavailable"))
            .when(pl.col(markout) > 0).then(pl.lit("favorable"))
            .when(pl.col(markout) < 0).then(pl.lit("adverse"))
            .otherwise(pl.lit("unchanged")).alias(outcome)
        )
        usable = events.filter(pl.col(valid))
        n = usable.height
        counts = {label: usable.filter(pl.col(outcome) == label).height
                  for label in ("favorable", "unchanged", "adverse")}
        assert sum(counts.values()) == n
        summary.append({"date": args.date, "horizon_ms": h, "trigger_count": events.height,
                        "valid_count": n, "unavailable_count": events.height - n,
                        **counts,
                        **{label + "_pct": 100 * count / n if n else None for label, count in counts.items()},
                        "mean_signed_markout_points": usable[markout].mean(),
                        "nonexact_matches": usable.filter(pl.col(delay) != 0).height})
    args.output.mkdir(parents=True, exist_ok=True)
    event_path = args.output / f"events_{args.date}.parquet"
    summary_path = args.output / f"summary_{args.date}.csv"
    events.write_parquet(event_path)
    pl.DataFrame(summary).write_csv(summary_path)
    manifest = {
        "date": args.date, "source_filename": args.input.name, "source_sha256": sha256(args.input),
        "evaluator_sha256": sha256(__file__), "python_version": platform.python_version(),
        "polars_version": pl.__version__, "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_rows": q.height, "rth_rows": rth.height, "rth_start_utc": start.isoformat(),
        "rth_end_utc_exclusive": end.isoformat(), "input_grid_ms": 50,
        "threshold_points": THRESHOLD, "horizons_ms": list(HORIZONS_MS),
        "maximum_forward_lookup_delay_ms": 50, "cooldown_ms": None,
        "timestamp_interpretation": "UTC, from the project's supplied convention; source timestamps are naive",
        "event_rule": "At/above threshold and preceding row below threshold, or sign flip while above threshold; no cooldown",
        "opening_state": "Pre-RTH grid row retained to avoid an artificial opening trigger",
        "synthetic_bars_retained": True, "synthetic_rth_rows": rth["is_synthetic_bar"].sum(),
        "output_sha256": {event_path.name: sha256(event_path), summary_path.name: sha256(summary_path)},
    }
    (args.output / f"manifest_{args.date}.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
