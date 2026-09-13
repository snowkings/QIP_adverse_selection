#!/usr/bin/env python3
"""Tick timing and hypothetical touch capture for existing QIP triggers.

Dependencies: numpy, polars; tzdata on Windows. No evaluator or renderer imports.

Example (console report only; net results omitted until costs are supplied):
  python tick_timing.py --ticks qip_tick_level.parquet \
      --events events_2026-07-10.parquet

Optional: --round-trip-cost-points COST --report report.json --rows rows.parquet
Costs are per-contract ES price points, covering fees and assumed extra slippage;
the bid/ask spread is already included. No files are written without output flags.

Frozen conventions for this first draft:
* Reuse the existing 50 ms bar-selected triggers. Do not generate tick triggers.
* DateTime is the bar-start label; decision time is DateTime + 50 ms.
* Primary lookup takes the last row STRICTLY BEFORE its clock time. An explicitly
  labeled at-or-before same-millisecond sensitivity is also reported. Equal-stamp
  rows retain file order. No future lookup, interpolation, or quote-age exclusion.
* Timing measures signed midpoint movement from the bar-close decision time.
* Capture enters at t0 + delay and exits at t0 + horizon: the exit stays fixed.
  remaining_hold_ms = horizon - delay; nonpositive values are unavailable.
* Midpoint adverse rates and gross/net capture loss rates are separate.
  Capture counts/rates are null outside capture mode; net also needs costs.
* Long entry/exit = ask/bid; short = bid/ask. Assume an infinitesimal order filled
  at recorded touches. No queue, impact, capacity, portfolio, or fill guarantee.
* Delay is a hypothetical total lag from the historical decision clock to action,
  not a measured routing latency. Timestamp origin/receipt latency are unverified.
* Full-length RTH only. Pre-close states may be observed at exactly 16:00 ET;
  no entry at/after that boundary. File completeness is an input assumption.
* July 10's [10:31, 10:37) ET split is retrospective and uses decision times.
  Whole-session results remain primary; the split never removes observations.
"""
import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl

MS = 1_000_000
BAR_MS = 50
NY = ZoneInfo("America/New_York")
TIMING_MS = (10, 20, 50, 100, 250, 500, 1000)
DELAYS_MS = (0, 5, 10, 20, 50, 100)
CAPTURE_MS = (100, 1000)
PRICES = ("qip", "mid", "best_bid", "best_ask")
REPORT_SCHEMA_VERSION = 2


def utc_ns(series):
    if not isinstance(series.dtype, pl.Datetime) or series.null_count():
        raise ValueError(f"{series.name} must contain non-null datetimes.")
    if series.dtype.time_zone is None:
        series = series.dt.replace_time_zone("UTC")
    return series.dt.convert_time_zone("UTC").dt.cast_time_unit("ns").cast(pl.Int64).to_numpy()


def clock(name, ns, valid=None):
    if valid is None:
        return pl.Series(name, ns).cast(pl.Datetime("ns", "UTC"))
    values = [int(value) if is_valid else None for value, is_valid in zip(ns, valid)]
    return pl.Series(name, values, dtype=pl.Datetime("ns", "UTC"))


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class TickBook:
    def __init__(self, path, start_ns, end_ns):
        schema = pl.scan_parquet(path).collect_schema()
        needed = {"timestamp", "DateTime", *PRICES}
        if needed - set(schema) or schema["timestamp"] != pl.Int64:
            raise ValueError("Ticks require timestamp (UTC epoch ns, Int64), DateTime and all PRICES.")
        raw = pl.read_parquet(path, columns=["timestamp", "DateTime", *PRICES])
        if raw.is_empty() or raw["timestamp"].null_count() or not raw["timestamp"].is_sorted():
            raise ValueError("Tick timestamps must be non-null and nondecreasing; file order is preserved.")
        # Internal consistency only; this does not establish exchange/receipt provenance.
        if not np.array_equal(raw["timestamp"].to_numpy(), utc_ns(raw["DateTime"])):
            raise ValueError("Tick timestamp disagrees with DateTime interpreted as UTC epoch ns.")
        self.start, self.end = start_ns, end_ns
        self.outside_rth_rows = raw.filter(
            (pl.col("timestamp") < start_ns) | (pl.col("timestamp") >= end_ns)).height
        raw = raw.filter((pl.col("timestamp") >= start_ns) & (pl.col("timestamp") < end_ns))
        valid = pl.all_horizontal([pl.col(c).is_finite() & (pl.col(c) > 0) for c in PRICES])
        valid = (valid & (pl.col("best_bid") <= pl.col("best_ask"))
                 & (pl.col("mid") == (pl.col("best_bid") + pl.col("best_ask")) / 2)).fill_null(False)
        if raw.is_empty() or not raw.select(valid.all()).item():
            raise ValueError("Missing RTH ticks or invalid RTH prices; refusing silent filtering.")
        self.ts = raw["timestamp"].to_numpy()
        self.price = {c: raw[c].to_numpy() for c in PRICES}
        if np.any(self.ts % MS):
            raise ValueError("This draft expects the verified millisecond-aligned source clock.")

    def at(self, target, at_or_before=False):
        index = np.searchsorted(self.ts, target, side="right" if at_or_before else "left") - 1
        valid = (index >= 0) & (target > self.start) & (target <= self.end)
        index = np.maximum(index, 0)
        return {"valid": valid, "ts": self.ts[index],
                "age_ms": (target - self.ts[index]) / MS,
                **{c: values[index] for c, values in self.price.items()}}


def load_inputs(ticks_path, events_path):
    events = pl.read_parquet(events_path)
    required = {"DateTime", "signal_dir", *PRICES}
    if required - set(events.columns) or events.is_empty():
        raise ValueError(f"Events require {sorted(required)} and at least one row.")
    labels = utc_ns(events["DateTime"])
    if np.any(np.diff(labels) <= 0) or np.any(labels % (BAR_MS * MS)):
        raise ValueError("Event labels must be unique, increasing 50 ms bar starts.")
    day = datetime.fromtimestamp(int(labels[0]) / 1e9, timezone.utc).astimezone(NY).date()
    start = int(datetime.combine(day, time(9, 30), NY).timestamp() * 1e9)
    end = int(datetime.combine(day, time(16), NY).timestamp() * 1e9)
    if np.any((labels < start) | (labels >= end)):
        raise ValueError("Events must belong to one regular full-length RTH session.")
    t0 = labels + BAR_MS * MS
    if "available_at" in events.columns and not np.array_equal(utc_ns(events["available_at"]), t0):
        raise ValueError("Existing available_at disagrees with DateTime + 50 ms.")
    direction = events["signal_dir"].to_numpy()
    displacement = events["qip"].to_numpy() - events["mid"].to_numpy()
    if (not np.isin(direction, [-1, 1]).all() or not np.isfinite(displacement).all()
            or np.any(np.abs(displacement) < .125) or np.any(direction != np.sign(displacement))):
        raise ValueError("Event direction or half-tick displacement is inconsistent.")
    book = TickBook(ticks_path, start, end)
    base = book.at(t0)
    match = base["valid"].copy()
    for c in PRICES:
        match &= base[c] == events[c].to_numpy()
    if not match.all():
        raise ValueError(f"Strict-before bar-close state matches {match.sum()}/{len(match)} triggers.")
    age = base["age_ms"]
    diagnostics = {
        "date": str(day), "trigger_count": len(t0), "trigger_state_matches": int(match.sum()),
        "rth_tick_rows": len(book.ts), "outside_rth_rows_ignored": book.outside_rth_rows,
        "adjacent_equal_timestamps": int(np.sum(np.diff(book.ts) == 0)),
        "trigger_state_age_ms_median": float(np.median(age)),
        "trigger_state_age_ms_p95": float(np.quantile(age, .95)),
        "trigger_state_age_ms_max": float(age.max()),
        "trigger_bins_without_update": int(np.sum(base["ts"] < labels)),
    }
    return events, labels, t0, direction, day, book, base, diagnostics


def scenario(lookup, mode, horizon, delay, events, labels, t0, direction, book, base, cost):
    entry_at, target_at = t0 + delay * MS, t0 + horizon * MS
    at_or_before = lookup == "at_or_before"
    entry, target = book.at(entry_at, at_or_before), book.at(target_at, at_or_before)
    valid = entry["valid"] & target["valid"] & (delay < horizon) & (entry_at < book.end)
    reason = np.where(delay >= horizon, "no_remaining_horizon",
                      np.where(target_at > book.end, "past_session_end",
                               np.where(entry_at >= book.end, "entry_at_or_after_close",
                                        np.where(valid, "", "missing_prior_state"))))
    mid_move = direction * (target["mid"] - entry["mid"])
    entry_price = np.where(direction > 0, entry["best_ask"], entry["best_bid"])
    exit_price = np.where(direction > 0, target["best_bid"], target["best_ask"])
    gross = direction * (exit_price - entry_price)
    capture_valid = valid & (mode == "capture")
    n = len(t0)
    rows = pl.DataFrame({
        "event_id": events["event_id"] if "event_id" in events.columns else np.arange(n),
        "lookup": [lookup] * n, "mode": [mode] * n, "horizon_ms": np.full(n, horizon),
        "delay_ms": np.full(n, delay), "remaining_hold_ms": np.full(n, horizon - delay),
        "bar_start": clock("bar_start", labels), "available_at": clock("available_at", t0),
        "source_tick_at": clock("source_tick_at", base["ts"]),
        "state_age_ms": base["age_ms"], "signal_dir": direction,
        "entry_at": clock("entry_at", entry_at), "target_at": clock("target_at", target_at),
        "entry_source_tick_at": clock("entry_source_tick_at", entry["ts"], valid),
        "target_source_tick_at": clock("target_source_tick_at", target["ts"], valid),
        "entry_state_age_ms": np.where(valid, entry["age_ms"], np.nan),
        "target_state_age_ms": np.where(valid, target["age_ms"], np.nan),
        "source_update_gap_ms": np.where(valid, (target["ts"] - entry["ts"]) / MS, np.nan),
        "valid": valid, "unavailable_reason": pl.Series("unavailable_reason", reason, dtype=pl.String),
        "signed_mid_move_points": np.where(valid, mid_move, np.nan),
        "entry_price": np.where(capture_valid, entry_price, np.nan),
        "exit_price": np.where(capture_valid, exit_price, np.nan),
        "gross_capture_points": np.where(capture_valid, gross, np.nan),
        "net_capture_points": np.where(capture_valid, gross - cost, np.nan)
                              if cost is not None else np.full(n, np.nan),
    })
    return rows.with_columns(pl.col(pl.Float64).fill_nan(None))


def summarize(rows, day, cost=None):
    cohorts = {"whole_session": rows}
    if str(day) == "2026-07-10":
        start = datetime.combine(day, time(10, 31), NY).astimezone(timezone.utc)
        end = datetime.combine(day, time(10, 37), NY).astimezone(timezone.utc)
        window = (pl.col("available_at") >= start) & (pl.col("available_at") < end)
        cohorts.update({"10:31-10:37_ET": rows.filter(window), "rest_of_session": rows.filter(~window)})
    tables = []
    move = pl.col("signed_mid_move_points")
    capture_columns = {prefix: pl.col(prefix + "_points") for prefix in ("gross_capture", "net_capture")}
    outcomes = ("positive", "zero", "negative")
    for label, data in cohorts.items():
        table = data.group_by("lookup", "mode", "horizon_ms", "delay_ms", "remaining_hold_ms").agg(
            pl.len().alias("trigger_count"), pl.col("valid").sum().alias("valid_count"),
            (~pl.col("valid")).sum().alias("unavailable_count"),
            (move > 0).sum().alias("midpoint_favorable"), (move == 0).sum().alias("midpoint_unchanged"),
            (move < 0).sum().alias("midpoint_adverse"), move.mean().alias("mean_mid_move_points"),
            *[test.sum().alias(f"{prefix}_{outcome}_count")
              for prefix, column in capture_columns.items()
              for outcome, test in zip(outcomes, (column > 0, column == 0, column < 0))],
            pl.col("gross_capture_points").mean().alias("mean_gross_capture_points"),
            pl.col("gross_capture_points").median().alias("median_gross_capture_points"),
            pl.col("gross_capture_points").quantile(.05).alias("p05_gross_capture_points"),
            pl.col("gross_capture_points").quantile(.95).alias("p95_gross_capture_points"),
            pl.col("net_capture_points").mean().alias("mean_net_capture_points"),
            pl.col("net_capture_points").median().alias("median_net_capture_points"),
            pl.col("entry_state_age_ms").quantile(.95).alias("entry_age_p95_ms"),
            pl.col("target_state_age_ms").quantile(.95).alias("target_age_p95_ms"),
            pl.col("target_state_age_ms").max().alias("target_age_max_ms"),
        ).with_columns(pl.lit(label).alias("cohort"))
        table = table.with_columns([
            pl.when(pl.col("valid_count") > 0).then(100 * pl.col(c) / pl.col("valid_count"))
              .otherwise(None).alias("midpoint_adverse_pct" if c == "midpoint_adverse" else c + "_pct")
            for c in ("midpoint_favorable", "midpoint_unchanged", "midpoint_adverse")])
        for prefix in capture_columns:
            applicable = (pl.col("mode") == "capture") & pl.lit(prefix == "gross_capture" or cost is not None)
            table = table.with_columns([
                pl.when(applicable).then(pl.col(f"{prefix}_{outcome}_count"))
                .otherwise(None).alias(f"{prefix}_{outcome}_count")
                for outcome in outcomes
            ]).with_columns([
                pl.when(applicable & (pl.col("valid_count") > 0))
                .then(100 * pl.col(f"{prefix}_{outcome}_count") / pl.col("valid_count"))
                .otherwise(None).alias(f"{prefix}_{outcome}_pct")
                for outcome in outcomes
            ])
        tables.append(table)
    summary = pl.concat(tables).sort("cohort", "lookup", "mode", "horizon_ms", "delay_ms")
    assert_summary_accounting(summary, cost)
    return summary


def assert_summary_accounting(summary, cost=None):
    for row in summary.to_dicts():
        if row["valid_count"] + row["unavailable_count"] != row["trigger_count"]:
            raise AssertionError(f"Availability accounting failed: {row}")
        if sum(row[f"midpoint_{outcome}"] for outcome in ("favorable", "unchanged", "adverse")) != row["valid_count"]:
            raise AssertionError(f"Midpoint accounting failed: {row}")
        if row["mode"] == "capture":
            if sum(row[f"gross_capture_{outcome}_count"] for outcome in ("positive", "zero", "negative")) != row["valid_count"]:
                raise AssertionError(f"Gross capture accounting failed: {row}")
            if cost is None:
                if any(row[f"net_capture_{outcome}_count"] is not None for outcome in ("positive", "zero", "negative")):
                    raise AssertionError(f"Net capture must be null when no cost is supplied: {row}")
            elif sum(row[f"net_capture_{outcome}_count"] for outcome in ("positive", "zero", "negative")) != row["valid_count"]:
                raise AssertionError(f"Net capture accounting failed: {row}")
        elif any(row[f"{prefix}_{outcome}_count"] is not None
                 for prefix in ("gross_capture", "net_capture") for outcome in ("positive", "zero", "negative")):
            raise AssertionError(f"Capture counts must be null outside capture mode: {row}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ticks", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--round-trip-cost-points", type=float,
                        help="Fees plus additional slippage in price points per contract; omitted = no net estimate.")
    parser.add_argument("--report", type=Path, help="Optional JSON settings, diagnostics and summaries.")
    parser.add_argument("--rows", type=Path, help="Optional per-event/per-scenario parquet; unavailable rows retained.")
    args = parser.parse_args()
    cost = args.round_trip_cost_points
    if cost is not None and (not np.isfinite(cost) or cost < 0):
        parser.error("Round-trip costs must be finite and nonnegative.")
    inputs = {args.ticks.resolve(), args.events.resolve(), Path(__file__).resolve()}
    outputs = [p.resolve() for p in (args.report, args.rows) if p is not None]
    if len(set(outputs)) != len(outputs) or any(p in inputs or p.exists() for p in outputs):
        parser.error("Output paths must be distinct, new files and must not overwrite inputs or this script.")
    events, labels, t0, direction, day, book, base, diagnostics = load_inputs(args.ticks, args.events)
    common = (events, labels, t0, direction, book, base, cost)
    frames = []
    for lookup in ("strict_before", "at_or_before"):
        frames += [scenario(lookup, "timing", h, 0, *common) for h in TIMING_MS]
        frames += [scenario(lookup, "capture", h, d, *common) for h in CAPTURE_MS for d in DELAYS_MS]
    rows = pl.concat(frames)
    summary = summarize(rows, day, cost)
    settings = {
        "report_schema_version": REPORT_SCHEMA_VERSION, "bar_ms": BAR_MS, "availability_offset_ms": BAR_MS,
        "primary_lookup": "strict_before: last tick with timestamp < target",
        "sensitivity_lookup": "at_or_before: last tick with timestamp <= target",
        "timestamp_resolution_ms": 1, "equal_timestamp_order": "preserve file order",
        "timing_horizons_ms": TIMING_MS, "capture_horizons_ms": CAPTURE_MS, "delays_ms": DELAYS_MS,
        "exit_clock": "t0 + horizon, independent of delay", "cost_points": cost,
        "quote_age_exclusions": False, "capture_model": "hypothetical recorded-touch fills, no size or queue model",
        "time_basis": "UTC source timestamps; feed latency unmeasured", "session": "regular RTH America/New_York",
        "focus_window": "July 10 only: [10:31, 10:37) ET, retrospective, grouped by available_at",
    }
    print(json.dumps(diagnostics, indent=2))
    print("Hypothetical touch fills; delay is assumed. Source completeness and live latency are unverified.")
    if cost is None:
        print("Costs not supplied: net capture is unavailable; gross capture includes the quoted spread only.")
    display = ["cohort", "lookup", "mode", "horizon_ms", "delay_ms", "remaining_hold_ms", "valid_count",
               "unavailable_count", "midpoint_adverse_pct", "mean_mid_move_points",
               "gross_capture_positive_pct", "gross_capture_negative_pct", "mean_gross_capture_points",
               "net_capture_positive_pct", "net_capture_negative_pct", "mean_net_capture_points"]
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=180, float_precision=4):
        print(summary.select(display))
        keys = ["cohort", "lookup", "mode", "horizon_ms", "delay_ms", "remaining_hold_ms",
                "valid_count", "unavailable_count"]
        for prefix in ("gross_capture", "net_capture"):
            if prefix == "net_capture" and cost is None:
                continue
            print(f"{prefix}: positive / zero / negative outcomes; denominator = valid_count.")
            columns = [f"{prefix}_{outcome}_{suffix}" for outcome in ("positive", "zero", "negative")
                       for suffix in ("count", "pct")]
            print(summary.filter(pl.col("mode") == "capture").select(keys + columns))
    if args.rows:
        args.rows.parent.mkdir(parents=True, exist_ok=True)
        rows.write_parquet(args.rows)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        hashes = {"ticks": digest(args.ticks), "events": digest(args.events), "script": digest(Path(__file__))}
        if args.rows:
            hashes["rows"] = digest(args.rows)
        report = {"settings": settings, "diagnostics": diagnostics, "summary": summary.to_dicts(),
                  "sha256": hashes,
                  "versions": {"python": sys.version.split()[0], "polars": pl.__version__, "numpy": np.__version__},
                  "platform": platform.platform(), "generated_utc": datetime.now(timezone.utc).isoformat()}
        args.report.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
