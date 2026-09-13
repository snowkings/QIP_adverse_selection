# QIP: short-horizon markouts and execution costs

On July 10, 2026, only **3.7% of 4,701 QIP trigger events** were followed by an adverse midpoint move at 100 ms. Favorable moves accounted for 37.6%; the midpoint was unchanged in 58.7% of windows. Among windows with a midpoint change, 91.1% moved with the signal.

This repository evaluates supplied QIP estimates as a potential quote-risk signal. It follows the evidence from directional midpoint behavior through a hypothetical aggressive execution test, with explicit clocks, spread costs and outcome counts. The QIP estimator itself is not included.

[![QIP session replay: ES midpoint and cumulative adverse rates](media/qip_review.png)](media/qip_review.mp4)

[Open the 25-second MP4](media/qip_review.mp4). Teal is 100 ms; amber is 1 second. The price line is sampled at trigger events. The percentage curves include every valid event and reveal each outcome at completed-bar availability.

## What the one-tick result means

The **median gross outcome is a loss of exactly one tick** at both 100 ms and 1 second. The **mean** losses are smaller: 0.79 tick and 0.74 tick, respectively. All tick conversions here use 0.25 ES index points per tick.

| Zero-delay scenario | Mean signed midpoint move | Mean entry/exit spread cost | Mean gross outcome | Median gross outcome |
| --- | ---: | ---: | ---: | ---: |
| Exit at 100 ms | +0.29 tick | 1.08 ticks | −0.79 tick | −1.00 tick |
| Exit at 1 second | +0.32 tick | 1.06 ticks | −0.74 tick | −1.00 tick |

For the recorded-touch model, the identity is:

**Gross outcome = signed midpoint movement − half the entry spread − half the exit spread.**

With a constant one-tick spread and no midpoint movement, buying at the ask and selling at the bid loses one tick in total. The observed spreads vary, so the mean spread cost is slightly more than one tick. Favorable average midpoint movement partly offsets that cost but does not cover it.

These results support a low incidence of adverse **midpoint markouts following QIP displacement on this session**. They do not establish that QIP eliminates adverse selection: there were 172 adverse midpoint outcomes at 100 ms, and no passive orders, actual fills or successful cancellations were measured. A claim of reduced adverse fills would require a comparison of a quoting policy with and without QIP.

## Directional behavior and aggressive capture

All entries below use the same 4,701 events, the primary strict-before clock and zero assumed action delay. Gross capture already includes the entry/exit spread; commissions and additional slippage are not supplied.

| Horizon | Midpoint favorable / unchanged / adverse | Gross capture positive / zero / negative | Mean gross outcome, points |
| --- | --- | --- | ---: |
| 100 ms | 37.6% / 58.7% / 3.7% | 0.9% / 26.2% / 72.9% | −0.197 |
| 1 second | 45.3% / 37.8% / 17.0% | 11.0% / 31.4% / 57.7% | −0.185 |

At 100 ms, the exact gross capture counts are **43 positive, 1,233 zero and 3,425 negative**. The median describes the middle observation; it does not mean every outcome loses one tick.

The tick study shows how quickly the average directional movement develops:

| Horizon after bar availability | Adverse midpoint rate | Mean signed midpoint move, points |
| --- | ---: | ---: |
| 10 ms | 0.9% | +0.037 |
| 20 ms | 1.3% | +0.050 |
| 50 ms | 2.4% | +0.066 |
| 100 ms | 3.7% | +0.073 |
| 250 ms | 6.8% | +0.085 |
| 500 ms | 11.1% | +0.088 |
| 1 second | 17.0% | +0.079 |

Roughly half the 100 ms average signed movement develops by 10 ms, and about 90% by 50 ms. Those are comparisons of averages, not execution windows guaranteed for individual events. In the delay sweep, the exit remains fixed at decision time plus the horizon; delaying entry reduces the remaining holding time. Average aggressive capture is negative at every valid tested delay and horizon.

## Stress interval and clock sensitivity

The retrospective **10:31–10:37 ET** interval contains 203 events and 61 of the session's 172 adverse 100 ms outcomes. Its adverse rate is 30.0%, versus 2.5% over the rest of the session. Both contribute to the headline 3.7%; the fast interval is retained throughout. The outside-window rate is not a corrected estimate.

The primary lookup uses the last tick strictly before each observation time. A separately labeled sensitivity includes all ticks stamped at that same millisecond. At zero delay, this changes mean gross capture from −0.197 to −0.208 points at 100 ms, and from −0.185 to −0.197 points at 1 second. The economic conclusion is unchanged under these two conventions. They are sensitivity cases, not guaranteed bounds on actual execution.

## Measurement contract

- **Session:** July 10, 2026, selected by recency before its outcomes were evaluated. RTH is 09:30–16:00 America/New_York, corresponding to 13:30–20:00 UTC on this date. This implementation handles regular full-length sessions.
- **Triggers:** existing 50 ms bar-selected crossings of `abs(qip - mid) >= 0.125` points. Retrigger after falling below the threshold or a direction flip while above it; no cooldown. The evaluator preserves the preceding pre-RTH state at the open.
- **Availability:** stored grid labels denote bar starts. Decisions become available at `DateTime + 50 ms`. The original event file retains its original labels. The tick study and this renderer apply the bar-close convention explicitly; shifting both endpoints leaves the grid horizons and classifications unchanged.
- **Tick reconstruction:** all 4,701 trigger QIP/mid/bid/ask states match the last tick strictly before bar close. Trigger-state age is 5 ms at the median, 30 ms at the 95th percentile and 50 ms at the maximum. Every trigger bin contains an update.
- **Source clock:** 5,760,236 valid RTH tick rows, with millisecond-aligned timestamps and duplicate timestamps in file order. Integer timestamps match `DateTime` interpreted as UTC. That checks internal consistency; source-event versus receipt-time provenance and live feed latency remain unmeasured. The file cannot resolve 400 microseconds.
- **Timing:** signed midpoint movement at 10/20/50/100/250/500/1,000 ms after availability. Lookups use the recorded prevailing state before the target, without waiting for a later update or excluding old quotes. Quote ages are reported. The optional at-or-before timing rows change the entry and target observations but keep the original trigger set and direction fixed.
- **Capture:** hypothetical infinitesimal long entry/exit at ask/bid, or short entry/exit at bid/ask. Entry is `t0 + delay`; exit is `t0 + horizon`. Delays are 0/5/10/20/50/100 ms; capture horizons are 100/1,000 ms. `remaining_hold_ms = horizon_ms - delay_ms`; nonpositive holds are unavailable.
- **Accounting:** favorable/unchanged/adverse midpoint counts and positive/zero/negative capture counts use their own explicit fields. Unavailable observations never become zero outcomes. Counts reconcile to each scenario's valid denominator. No cost argument means all net capture metrics are null; supplying zero costs is a different, explicit assumption.
- **Scope:** one-session descriptive evidence. Events can cluster and overlap; hypothetical outcomes are not an executable portfolio. Actual fills, size, queue priority, impact, cancellation success and a baseline quoting policy are not modeled. No event-independent confidence interval or fair-value superiority claim is made.

## Files and reproduction

| File | Purpose |
| --- | --- |
| `tick_timing.py` | Current tick timing and recorded-touch capture study |
| `test_tick_timing.py` | Seven supplied boundary, serialization and accounting tests |
| `evaluate.py` | Original 50 ms trigger/grid evaluator, preserved unchanged |
| `render.py` | Three-line renderer with matching legend colors and completed-bar clocks |
| `pyproject.toml`, `poetry.lock` | Locked dependencies and Poetry configuration |
| `results/events_2026-07-10.parquet` | Original 4,701 trigger events and grid outcomes |
| `results/summary_2026-07-10.csv` | Original five-horizon grid summary; 5 seconds has 4,700 valid events |
| `results/manifest_2026-07-10.json` | Original grid-run provenance, preserved unchanged |
| `results/report_2026-07-10.json` | Refreshed current-script tick report, with costs omitted and source/script hashes |
| `media/` | MP4 replay and closing-frame preview |

The report was regenerated from the included `tick_timing.py`; its recorded script hash matches this file. The current report uses the same source tick and event hashes as the supplied Windows run. The original grid manifest documents the earlier evaluator run; the availability interpretation above supplements its historical bar-label convention.

Use Python 3.12 or later. These commands run from the repository root. Python dependencies are locked in `pyproject.toml` and `poetry.lock`. The included `imageio-ffmpeg` dependency supplies the renderer's FFmpeg binary.

```powershell
poetry install
poetry run python -m unittest test_tick_timing -q
poetry run python tick_timing.py --ticks "C:\Projects\SPX\data\processed_data\qip_outputs\2026-07-10\qip_tick_level.parquet" --events results/events_2026-07-10.parquet
```

The study prints results without writing files by default. To save a new report, add `--report results/report_local.json`; existing output paths are refused. Raw QIP source parquets remain external inputs. The included events suffice to render the replay:

```powershell
poetry run python render.py --events results/events_2026-07-10.parquet --output media/qip_review_local.mp4
```

The renderer overwrites a specified existing output, so use a new name when keeping an earlier version. Its percentage axis includes the full observed cumulative-rate peak; it does not clip early volatility. Full-session statistics and the retrospective stress annotation appear during the closing hold.

To rebuild the original event file from a complete private 50 ms grid, use a separate output directory:

```powershell
poetry run python evaluate.py --input "C:\Projects\SPX\data\processed_data\qip_outputs\2026-07-10\qip_50ms.parquet" --date 2026-07-10 --output results/grid_rerun
```

