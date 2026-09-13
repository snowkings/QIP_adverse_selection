"""Boundary tests for tick_timing's frozen clock and capture semantics."""

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).parent))
from tick_timing import MS, TickBook, assert_summary_accounting, scenario


def tick_frame(start_ns: int) -> pl.DataFrame:
    timestamps = [start_ns, start_ns + 10 * MS, start_ns + 10 * MS, start_ns + 20 * MS]
    bids = [100.0, 100.25, 100.50, 100.75]
    asks = [100.25, 100.50, 100.75, 101.00]
    return pl.DataFrame(
        {
            "timestamp": timestamps,
            "qip": [100.125, 100.375, 100.625, 100.875],
            "mid": [100.125, 100.375, 100.625, 100.875],
            "best_bid": bids,
            "best_ask": asks,
        }
    ).with_columns(pl.from_epoch("timestamp", time_unit="ns").dt.replace_time_zone("UTC").alias("DateTime"))


class TickBookTests(unittest.TestCase):
    def setUp(self):
        self.start = 1_000 * MS
        self.end = self.start + 30 * MS
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "ticks.parquet"
        tick_frame(self.start).write_parquet(self.path)
        self.book = TickBook(self.path, self.start, self.end)

    def tearDown(self):
        self.directory.cleanup()

    def test_strict_before_excludes_equal_timestamp_rows(self):
        state = self.book.at(np.array([self.start + 10 * MS]))
        self.assertEqual(state["ts"][0], self.start)
        self.assertEqual(state["best_bid"][0], 100.0)

    def test_at_or_before_uses_last_row_at_equal_timestamp(self):
        state = self.book.at(np.array([self.start + 10 * MS]), at_or_before=True)
        self.assertEqual(state["ts"][0], self.start + 10 * MS)
        self.assertEqual(state["best_bid"][0], 100.50)

    def test_close_boundary_allows_observation_but_not_later_clock(self):
        at_close = self.book.at(np.array([self.end]))
        after_close = self.book.at(np.array([self.end + MS]))
        self.assertTrue(at_close["valid"][0])
        self.assertFalse(after_close["valid"][0])


class AccountingTests(unittest.TestCase):
    def test_scenario_rows_are_parquet_serializable(self):
        start = 1_000 * MS
        end = start + 30 * MS
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ticks.parquet"
            tick_frame(start).write_parquet(path)
            book = TickBook(path, start, end)
            labels = np.array([start])
            t0 = labels + 5 * MS
            base = book.at(t0)
            events = pl.DataFrame({"event_id": [0]})
            rows = scenario(
                "strict_before", "capture", 10, 0, events, labels, t0, np.array([1]), book, base, 0.0
            )
            self.assertNotIn(pl.Object, rows.schema.values())
            rows.write_parquet(Path(directory) / "rows.parquet")

    def test_summary_accounting_accepts_complete_counts(self):
        summary = pl.DataFrame(
            {
                "trigger_count": [3], "valid_count": [2], "unavailable_count": [1],
                "midpoint_favorable": [1], "midpoint_unchanged": [0], "midpoint_adverse": [1],
                "mode": ["capture"], "gross_capture_positive_count": [0], "gross_capture_zero_count": [1],
                "gross_capture_negative_count": [1], "net_capture_positive_count": [0], "net_capture_zero_count": [1],
                "net_capture_negative_count": [1],
            }
        )
        assert_summary_accounting(summary, cost=0.0)

    def test_summary_accounting_rejects_incomplete_capture_counts(self):
        summary = pl.DataFrame(
            {
                "trigger_count": [1], "valid_count": [1], "unavailable_count": [0],
                "midpoint_favorable": [1], "midpoint_unchanged": [0], "midpoint_adverse": [0],
                "mode": ["capture"], "gross_capture_positive_count": [0], "gross_capture_zero_count": [0],
                "gross_capture_negative_count": [0], "net_capture_positive_count": [0], "net_capture_zero_count": [0],
                "net_capture_negative_count": [0],
            }
        )
        with self.assertRaises(AssertionError):
            assert_summary_accounting(summary, cost=0.0)

    def test_summary_accounting_accepts_null_net_counts_without_cost(self):
        summary = pl.DataFrame(
            {
                "trigger_count": [1], "valid_count": [1], "unavailable_count": [0],
                "midpoint_favorable": [1], "midpoint_unchanged": [0], "midpoint_adverse": [0],
                "mode": ["capture"], "gross_capture_positive_count": [0], "gross_capture_zero_count": [0],
                "gross_capture_negative_count": [1], "net_capture_positive_count": [None],
                "net_capture_zero_count": [None], "net_capture_negative_count": [None],
            }
        )
        assert_summary_accounting(summary, cost=None)


if __name__ == "__main__":
    unittest.main()