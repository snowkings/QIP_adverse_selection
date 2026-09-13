#!/usr/bin/env python3
"""Render a review MP4 from existing QIP event results; never run the evaluator.

Dependencies: polars, numpy, matplotlib; ffmpeg on PATH or imageio-ffmpeg.
Windows also needs timezone data (tzdata), as does the existing evaluator.

Example (price sampled at trigger timestamps):
  python render.py --events events_2026-07-10.parquet --output qip_review.mp4

Example (complete price grid):
  python render.py --events events_2026-07-10.parquet \
      --price-grid qip_50ms.parquet --output qip_review.mp4

Only the requested MP4 and optional still PNG are written. Input files are
read-only. Three data lines: midpoint, cumulative adverse rate at 100 ms,
and cumulative adverse rate at 1 s. The two rates are NOT mean markouts.
All valid event outcomes enter their horizon's denominator. Input DateTime and
future_{h}ms label the START of completed 50 ms bars; both are shifted +50 ms
for display and outcome availability. Horizon lengths and outcomes are unchanged.
The July 10 closing hold highlights decision times [10:31, 10:37) ET. This
fixed retrospective annotation counts valid adverse outcomes at 100 ms;
it does not exclude events or change either cumulative rate.
"""
import argparse
import math
import shutil
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.animation import FFMpegWriter
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, MaxNLocator
import numpy as np
import polars as pl

BG, PANEL = "#101925", "#14202f"
TEXT, MUTED, GRID = "#edf2f8", "#a1b0c4", "#314054"
PRICE, FAST, SLOW = "#dce7f5", "#48d8c0", "#ffc078"
NY = ZoneInfo("America/New_York")
HORIZONS = (100, 1000)
BAR_NS = 50_000_000


def timestamp_ns(series):
    """Return nanosecond UTC timestamps, interpreting naive input as UTC."""
    if not isinstance(series.dtype, pl.Datetime):
        raise ValueError(f"Expected timestamps, got {series.dtype}")
    if series.dtype.time_zone is None:
        series = series.dt.replace_time_zone("UTC")
    return series.dt.convert_time_zone("UTC").dt.cast_time_unit("ns").cast(pl.Int64).to_numpy()


def load_inputs(event_path, price_path):
    events = pl.read_parquet(event_path)
    required = {"DateTime", "mid"}
    for h in HORIZONS:
        required.update((f"valid_{h}ms", f"markout_{h}ms", f"future_{h}ms"))
    missing = required - set(events.columns)
    if missing or events.is_empty():
        raise ValueError(f"Missing event data: {sorted(missing)}")
    events = events.sort("DateTime")
    label_ns = timestamp_ns(events["DateTime"])
    if np.any(np.diff(label_ns) <= 0) or np.any(label_ns % BAR_NS):
        raise ValueError("Trigger labels must be unique, increasing 50 ms bar starts.")
    trigger_ns = label_ns + BAR_NS
    day = datetime.fromtimestamp(int(trigger_ns[0]) / 1e9, timezone.utc).astimezone(NY).date()
    start = datetime.combine(day, time(9, 30), NY)
    end = datetime.combine(day, time(16, 0), NY)
    start_ns, end_ns = int(start.timestamp() * 1e9), int(end.timestamp() * 1e9)
    if np.any((label_ns < start_ns) | (label_ns >= end_ns)):
        raise ValueError("This renderer expects one full-length RTH session's events.")
    source = {"events": events, "day": day, "start": start, "end": end,
              "start_ns": start_ns, "trigger_minutes": (trigger_ns - start_ns) / 60e9,
              "total": events.height, "rates": {}, "highlight": None}
    for h in HORIZONS:
        usable = events.filter(pl.col(f"valid_{h}ms")).sort(f"future_{h}ms")
        if usable.is_empty():
            raise ValueError(f"No valid outcomes at {h} ms.")
        ready_ns = timestamp_ns(usable[f"future_{h}ms"]) + BAR_NS
        origin_ns = timestamp_ns(usable["DateTime"]) + BAR_NS
        values = usable[f"markout_{h}ms"].to_numpy()
        if not np.isfinite(values).all():
            raise ValueError(f"Non-finite valid markout at {h} ms.")
        if np.any(ready_ns < origin_ns + h * 1_000_000) or np.any(ready_ns > end_ns):
            raise ValueError(f"Invalid within-session availability timestamp at {h} ms.")
        cumulative = np.cumsum(values < 0)
        rate = 100.0 * cumulative / np.arange(1, len(values) + 1)
        source["rates"][h] = {
            "minutes": (ready_ns - start_ns) / 60e9, "rates": rate,
            "adverse_counts": cumulative, "n": len(values),
            "favorable": int(np.sum(values > 0)), "unchanged": int(np.sum(values == 0)),
            "adverse": int(np.sum(values < 0)), "final": float(rate[-1]),
        }
        if h == 100 and day.isoformat() == "2026-07-10":
            # Fixed review window, selected retrospectively; group by decision time.
            window_start = datetime.combine(day, time(10, 31), NY)
            window_end = datetime.combine(day, time(10, 37), NY)
            window_start_ns = int(window_start.timestamp() * 1e9)
            window_end_ns = int(window_end.timestamp() * 1e9)
            inside = (origin_ns >= window_start_ns) & (origin_ns < window_end_ns)
            source["highlight"] = {
                "start": (window_start_ns - start_ns) / 60e9,
                "end": (window_end_ns - start_ns) / 60e9,
                "adverse": int(np.sum(inside & (values < 0))),
                "total_adverse": source["rates"][h]["adverse"],
            }
    if price_path:
        prices = pl.read_parquet(price_path, columns=["DateTime", "mid"])
        pns = timestamp_ns(prices["DateTime"])
        keep = (pns >= start_ns) & (pns < end_ns)
        pns, py = pns[keep], prices["mid"].to_numpy()[keep]
        if len(pns) != 468000 or pns[0] != start_ns or pns[-1] != end_ns - 50_000_000:
            raise ValueError("Price input does not cover the complete 50 ms RTH grid.")
        if np.any(np.diff(pns) != 50_000_000) or not np.isfinite(py).all() or np.any(py <= 0):
            raise ValueError("Price grid has gaps or invalid midpoints.")
        idx = np.searchsorted(pns, label_ns)
        if not np.array_equal(pns[idx], label_ns) or not np.array_equal(py[idx], events["mid"].to_numpy()):
            raise ValueError("Price input does not match the recorded trigger midpoints.")
        pns = pns + BAR_NS
        source["price_basis"] = "Complete 50 ms price grid"
    else:
        pns, py = trigger_ns, events["mid"].to_numpy()
        if not np.isfinite(py).all() or np.any(py <= 0):
            raise ValueError("Invalid trigger midpoints.")
        source["price_basis"] = "Price sampled at QIP trigger timestamps"
    source["price_minutes"] = (pns - start_ns) / 60e9
    source["price"] = py
    return source


class ReviewFigure:
    def __init__(self, data, width, height, duration, closing):
        self.data, self.duration, self.closing = data, duration, closing
        self.replay = duration - closing
        plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 12,
                             "text.color": TEXT, "axes.labelcolor": MUTED,
                             "xtick.color": MUTED, "ytick.color": MUTED,
                             "path.simplify": False, "savefig.facecolor": BG})
        self.fig = plt.figure(figsize=(16, 9), dpi=width / 16, facecolor=BG)
        # A single shared clock, with separate price and percentage scales.
        self.price_ax = self.fig.add_axes([.083, .550, .650, .235], facecolor=PANEL)
        self.rate_ax = self.fig.add_axes([.083, .230, .650, .235], facecolor=PANEL,
                                       sharex=self.price_ax)
        self.price_ax.set_xlim(0, 390)
        self.price_ax.tick_params(axis="x", labelbottom=False, bottom=False)
        for ax in (self.price_ax, self.rate_ax):
            ax.spines[["top", "right", "left"]].set_visible(False)
            ax.spines["bottom"].set_color(GRID)
            ax.tick_params(axis="both", length=0, pad=10)
            ax.grid(axis="y", color=GRID, linewidth=.65, alpha=.6)
            ax.set_axisbelow(True)
        py = data["price"]
        pad = max(float(np.ptp(py)) * .10, 1.0)
        self.price_ax.set_ylim(float(py.min()) - pad, float(py.max()) + pad)
        self.price_ax.yaxis.set_major_locator(MaxNLocator(4))
        self.price_ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:,.0f}"))
        rate_max = max(float(data["rates"][h]["rates"].max()) for h in HORIZONS)
        upper = max(20, math.ceil((rate_max + 2) / 5) * 5)
        self.rate_ax.set_ylim(0, upper)
        self.rate_ax.yaxis.set_major_locator(MaxNLocator(4, integer=True))
        self.rate_ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{y:g}%"))
        ticks = [0, 90, 210, 330, 390]
        self.rate_ax.set_xticks(ticks, ["09:30", "11:00", "13:00", "15:00", "16:00"])
        self.rate_ax.set_xlabel("Session clock · New York time", fontsize=11, labelpad=13)
        self.price_line, = self.price_ax.plot([], [], color=PRICE, lw=1.35)
        self.price_tip, = self.price_ax.plot([], [], "o", color=PRICE, ms=4)
        self.rate_lines, self.rate_tips = {}, {}
        for h, color in ((100, FAST), (1000, SLOW)):
            self.rate_lines[h], = self.rate_ax.plot([], [], color=color, lw=1.9, drawstyle="steps-post")
            self.rate_tips[h], = self.rate_ax.plot([], [], "o", color=color, ms=4)
        self.cursors = [ax.axvline(0, color=TEXT, alpha=.30, lw=.8) for ax in (self.price_ax, self.rate_ax)]
        self.highlight_artists = []
        highlight = data["highlight"]
        if highlight is not None:
            # Reveal session-wide counts only during the closing hold.
            band = self.price_ax.axvspan(
                highlight["start"], highlight["end"], color=SLOW,
                alpha=.16, linewidth=0, zorder=0, visible=False)
            label = self.price_ax.annotate(
                "Retrospective highlight · 10:31–10:37 ET\n"
                f"{highlight['adverse']:,} of {highlight['total_adverse']:,} "
                "session adverse outcomes · 100 ms",
                xy=(highlight["end"], .96),
                xycoords=self.price_ax.get_xaxis_transform(),
                xytext=(8, 0), textcoords="offset points", va="top",
                fontsize=9, color=TEXT, visible=False,
                bbox={"facecolor": PANEL, "edgecolor": "none", "alpha": .9})
            self.highlight_artists = [band, label]
        self.fig.text(.083, .935, "QIP  /  SINGLE-SESSION REVIEW", color=MUTED, fontsize=12, weight="bold")
        self.fig.text(.083, .872, "Adverse markouts, two horizons", fontsize=28, weight="bold")
        self.fig.text(.083, .832, f"ES  ·  {data['day'].strftime('%B %d, %Y')}  ·  09:30–16:00 ET", color=MUTED, fontsize=12)
        self.fig.text(.083, .792, "ES MIDPOINT", fontsize=10, weight="bold", color=PRICE)
        self.fig.text(.733, .792, data["price_basis"], ha="right", color=MUTED, fontsize=9)
        self.fig.text(.083, .504, "CUMULATIVE ADVERSE RATE", fontsize=10, weight="bold")
        self.fig.text(.733, .504, "Negative outcomes / available outcomes", ha="right", color=MUTED, fontsize=9)
        self.rate_ax.legend(handles=[Line2D([0], [0], color=FAST, lw=2, label="100 ms"),
                                    Line2D([0], [0], color=SLOW, lw=2, label="1 second")],
                            loc="upper right", ncol=2, frameon=False, fontsize=10,
                            labelcolor=[FAST, SLOW], borderaxespad=.7)
        self.fig.add_artist(Line2D([.771, .771], [.230, .800], transform=self.fig.transFigure,
                                  color=GRID, lw=.8))
        self.clock_label = self.fig.text(.805, .775, "SESSION CLOCK · ET", color=MUTED, fontsize=10, weight="bold")
        self.clock = self.fig.text(.805, .725, "09:30:00", fontsize=25, weight="bold")
        self.fig.text(.805, .651, "TRIGGER EVENTS", color=MUTED, fontsize=10, weight="bold")
        self.count = self.fig.text(.805, .605, "0", fontsize=25, weight="bold")
        self.fig.text(.805, .575, f"of {data['total']:,} in this session", color=MUTED, fontsize=10)
        self.rate_text, self.denominator_text = {}, {}
        for h, color, y, label in ((100, FAST, .472, "100 ms"), (1000, SLOW, .316, "1 second")):
            self.fig.text(.805, y, label, color=color, fontsize=13, weight="bold")
            self.rate_text[h] = self.fig.text(.805, y - .050, "—", color=color, fontsize=29, weight="bold")
            self.denominator_text[h] = self.fig.text(.805, y - .080, "No outcomes yet", color=MUTED, fontsize=9)
        self.note = self.fig.text(.083, .128, "Same trigger events. Two look-ahead horizons.", fontsize=13, weight="bold")
        self.note2 = self.fig.text(.083, .094, "Outcomes enter each curve only when their forward midpoint is available.", color=MUTED, fontsize=11)
        self.summary_text = {
            100: self.fig.text(.083, .094, "", color=FAST, fontsize=11, visible=False),
            1000: self.fig.text(.405, .094, "", color=SLOW, fontsize=11, visible=False),
        }
        self.fig.text(.083, .044, "Half-tick crossing  |QIP − mid| ≥ 0.125  ·  No cooldown  ·  Signed midpoint outcomes", color=MUTED, fontsize=9)
        self.fig.text(.944, .044, "Single-session evidence", color=MUTED, fontsize=9, ha="right")

    def update(self, video_seconds):
        closing = video_seconds >= self.replay
        minute = 390 * min(max(video_seconds / self.replay, 0), 1)
        data = self.data
        px, py = data["price_minutes"], data["price"]
        nprice = int(np.searchsorted(px, minute, side="right"))
        self.price_line.set_data(px[:nprice], py[:nprice])
        self.price_tip.set_data(px[nprice-1:nprice] if nprice else [], py[nprice-1:nprice] if nprice else [])
        for cursor in self.cursors:
            cursor.set_xdata([minute, minute])
        clock = data["start"] + timedelta(minutes=minute)
        self.clock.set_text(clock.strftime("%H:%M:%S"))
        self.clock_label.set_text("SESSION COMPLETE · ET" if closing else "SESSION CLOCK · ET")
        self.count.set_text(f"{np.searchsorted(data['trigger_minutes'], minute, side='right'):,}")
        for h in HORIZONS:
            series = data["rates"][h]
            n = int(np.searchsorted(series["minutes"], minute, side="right"))
            if closing and n != series["n"]:
                raise ValueError(
                    f"Incomplete closing frame at {h} ms: {n}/{series['n']} outcomes.")
            if n:
                # Extend the last known cumulative rate to the cursor, with no future data.
                x = np.r_[series["minutes"][:n], minute]
                y = np.r_[series["rates"][:n], series["rates"][n - 1]]
                self.rate_lines[h].set_data(x, y)
                self.rate_tips[h].set_data([minute], [y[-1]])
                self.rate_text[h].set_text(f"{y[-1]:.1f}%")
                self.denominator_text[h].set_text(f"{series['adverse_counts'][n-1]:,} adverse / {n:,} available")
            else:
                self.rate_lines[h].set_data([], [])
                self.rate_tips[h].set_data([], [])
                self.rate_text[h].set_text("—")
                self.denominator_text[h].set_text("No outcomes yet")
        for artist in self.highlight_artists:
            artist.set_visible(closing)
        self.note2.set_visible(not closing)
        for text in self.summary_text.values():
            text.set_visible(closing)
        if closing:
            self.note.set_text("FULL SESSION  ·  Favorable / unchanged / adverse")
            a, b = data["rates"][100], data["rates"][1000]
            def split(s):
                return " / ".join(f"{100*s[k]/s['n']:.1f}%" for k in ("favorable", "unchanged", "adverse"))
            self.summary_text[100].set_text(f"100 ms: {split(a)}")
            self.summary_text[1000].set_text(f"1 second: {split(b)}")
        else:
            self.note.set_text("Same trigger events. Two look-ahead horizons.")
            self.note2.set_text("Outcomes enter each curve only when their forward midpoint is available.")


def ffmpeg_path(explicit):
    if explicit:
        path = str(explicit)
    else:
        path = shutil.which("ffmpeg")
        if not path:
            try:
                import imageio_ffmpeg
                path = imageio_ffmpeg.get_ffmpeg_exe()
            except ImportError as ex:
                raise RuntimeError("Install ffmpeg or imageio-ffmpeg, or supply --ffmpeg.") from ex
    if not Path(path).is_file():
        raise FileNotFoundError(path)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--events", required=True, type=Path,
        help="One-session event parquet: DateTime, mid, and valid_{h}ms (boolean), "
             "markout_{h}ms (signed points), future_{h}ms (matched quote timestamp), "
             "for h=100 and 1000. These are 50 ms bar-start labels, shifted +50 ms "
             "to completed-bar availability. Naive timestamps are interpreted as UTC.")
    parser.add_argument(
        "--price-grid", type=Path,
        help="Optional DateTime/mid parquet covering 09:30–16:00 America/New_York "
             "on the event date: 468,000 rows at exact 50 ms spacing, close excluded. "
             "Trigger timestamps and midpoints must match the events exactly; "
             "file bytes need not match. Otherwise use labeled trigger midpoints.")
    parser.add_argument("--output", type=Path, default=Path("qip_review.mp4"))
    parser.add_argument("--duration", type=float, default=25, help="Total seconds including closing hold")
    parser.add_argument("--closing-seconds", type=float, default=4)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--ffmpeg", type=Path)
    parser.add_argument("--still", type=Path, help="Optional final-frame PNG")
    parser.add_argument("--still-only", action="store_true", help="Review layout without encoding; requires --still")
    args = parser.parse_args()
    if args.width * 9 != args.height * 16 or args.width % 2 or args.height % 2:
        parser.error("Use even 16:9 dimensions, for example 1920×1080 or 1280×720.")
    if args.fps <= 0 or args.duration <= args.closing_seconds or args.closing_seconds < 0:
        parser.error("Require positive fps and duration greater than the nonnegative closing hold.")
    if args.still_only and not args.still:
        parser.error("--still-only requires --still.")
    input_paths = {args.events.resolve()}
    if args.price_grid:
        input_paths.add(args.price_grid.resolve())
    if args.output.resolve() in input_paths or (args.still and args.still.resolve() in input_paths):
        parser.error("An output path cannot overwrite an input file.")
    data = load_inputs(args.events, args.price_grid)
    view = ReviewFigure(data, args.width, args.height, args.duration, args.closing_seconds)
    print(f"{data['day']}: {data['total']:,} trigger events | {data['price_basis']}", flush=True)
    for h in HORIZONS:
        s = data["rates"][h]
        print(f"{h} ms: {s['adverse']}/{s['n']} adverse = {s['final']:.6f}%", flush=True)
    if args.still:
        args.still.parent.mkdir(parents=True, exist_ok=True)
        view.update(args.duration)
        view.fig.savefig(args.still, dpi=args.width / 16, facecolor=BG)
        print(f"Still: {args.still}", flush=True)
    if not args.still_only:
        matplotlib.rcParams["animation.ffmpeg_path"] = ffmpeg_path(args.ffmpeg)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        writer = FFMpegWriter(fps=args.fps, codec="libx264", bitrate=-1,
                             extra_args=["-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
                                         "-movflags", "+faststart", "-threads", "2"],
                             metadata={"title": f"QIP — {data['day']} — 100 ms and 1 second",
                                       "comment": data["price_basis"]})
        frames = round(args.duration * args.fps)
        with writer.saving(view.fig, str(args.output), dpi=args.width / 16):
            for i in range(frames):
                view.update(i / args.fps)
                writer.grab_frame(facecolor=BG)
                if i % (5 * args.fps) == 0:
                    print(f"Rendering: {i}/{frames} frames", flush=True)
        print(f"MP4: {args.output} ({args.output.stat().st_size / 1e6:.2f} MB)", flush=True)
    plt.close(view.fig)


if __name__ == "__main__":
    main()
