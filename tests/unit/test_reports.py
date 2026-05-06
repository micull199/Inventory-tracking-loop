"""Unit tests for the reports surface (R4).

Pure helpers in ``app/reports.py``. No DB writes, no fixtures, no app wiring.
The integration tests in ``tests/integration/test_reports_routes.py`` exercise
the route surface end-to-end; this file covers the standalone helpers so a
regression in the rollup arithmetic is caught even before the route is wired.
"""

from __future__ import annotations

from decimal import Decimal

from app.models import StockTakeLine
from app.reports import _aggregate_lines, _coerce_days, _combine_aggregates

# ---------------------------------------------------------------------------
# _coerce_days
# ---------------------------------------------------------------------------


class TestCoerceDays:
    def test_blank_returns_default(self) -> None:
        assert _coerce_days("") == 90

    def test_none_returns_default(self) -> None:
        assert _coerce_days(None) == 90

    def test_non_int_returns_default(self) -> None:
        assert _coerce_days("foo") == 90

    def test_below_min_returns_default(self) -> None:
        assert _coerce_days("0") == 90

    def test_above_max_returns_default(self) -> None:
        assert _coerce_days("9999") == 90

    def test_valid_value_passes_through(self) -> None:
        assert _coerce_days("30") == 30

    def test_min_value_passes_through(self) -> None:
        assert _coerce_days("1") == 1

    def test_max_value_passes_through(self) -> None:
        assert _coerce_days("365") == 365


# ---------------------------------------------------------------------------
# _aggregate_lines
# ---------------------------------------------------------------------------


def _line(
    *,
    committed: bool = True,
    variance: Decimal | None = Decimal("0"),
) -> StockTakeLine:
    """Construct a transient line for aggregate testing.

    Not committed via the session — the helpers operate on Python objects, not
    DB state. ``stock_take_id`` and ``item_id`` are placeholders.
    """
    return StockTakeLine(
        stock_take_id=1,
        item_id=1,
        system_qty=Decimal("10.0000"),
        counted_qty=Decimal("10.0000"),
        variance=variance,
        committed=committed,
    )


class TestAggregateLines:
    def test_empty_list_returns_zeros(self) -> None:
        agg = _aggregate_lines([])
        assert agg == {
            "lines_with_variance": 0,
            "positive_variance": Decimal("0"),
            "negative_variance_abs": Decimal("0"),
            "net_variance": Decimal("0"),
            "abs_variance": Decimal("0"),
        }

    def test_uncommitted_lines_excluded(self) -> None:
        """Variances that haven't been actioned through the engine don't count."""
        lines = [
            _line(committed=False, variance=Decimal("5")),
            _line(committed=False, variance=Decimal("-3")),
        ]
        agg = _aggregate_lines(lines)
        assert agg["lines_with_variance"] == 0
        assert agg["positive_variance"] == Decimal("0")
        assert agg["negative_variance_abs"] == Decimal("0")
        assert agg["net_variance"] == Decimal("0")
        assert agg["abs_variance"] == Decimal("0")

    def test_zero_variance_committed_lines_excluded(self) -> None:
        """A committed line with no variance contributes nothing."""
        lines = [_line(committed=True, variance=Decimal("0"))]
        assert _aggregate_lines(lines)["lines_with_variance"] == 0

    def test_none_variance_excluded(self) -> None:
        lines = [_line(committed=True, variance=None)]
        assert _aggregate_lines(lines)["lines_with_variance"] == 0

    def test_only_positive_lines(self) -> None:
        lines = [
            _line(committed=True, variance=Decimal("3")),
            _line(committed=True, variance=Decimal("4")),
        ]
        agg = _aggregate_lines(lines)
        assert agg["lines_with_variance"] == 2
        assert agg["positive_variance"] == Decimal("7")
        assert agg["negative_variance_abs"] == Decimal("0")
        assert agg["net_variance"] == Decimal("7")
        assert agg["abs_variance"] == Decimal("7")

    def test_only_negative_lines(self) -> None:
        lines = [
            _line(committed=True, variance=Decimal("-2")),
            _line(committed=True, variance=Decimal("-5")),
        ]
        agg = _aggregate_lines(lines)
        assert agg["lines_with_variance"] == 2
        assert agg["positive_variance"] == Decimal("0")
        assert agg["negative_variance_abs"] == Decimal("7")
        assert agg["net_variance"] == Decimal("-7")
        assert agg["abs_variance"] == Decimal("7")

    def test_mixed_lines(self) -> None:
        lines = [
            _line(committed=True, variance=Decimal("3")),
            _line(committed=True, variance=Decimal("-5")),
            _line(committed=True, variance=Decimal("0")),  # excluded
            _line(committed=False, variance=Decimal("99")),  # excluded
        ]
        agg = _aggregate_lines(lines)
        assert agg["lines_with_variance"] == 2
        assert agg["positive_variance"] == Decimal("3")
        assert agg["negative_variance_abs"] == Decimal("5")
        assert agg["net_variance"] == Decimal("-2")  # 3 + (-5) = -2
        assert agg["abs_variance"] == Decimal("8")  # |3| + |-5| = 8

    def test_abs_equals_positive_plus_negative_abs(self) -> None:
        """Invariant: ``abs_variance == positive_variance + negative_variance_abs``."""
        lines = [
            _line(committed=True, variance=Decimal("4")),
            _line(committed=True, variance=Decimal("-7")),
            _line(committed=True, variance=Decimal("2")),
        ]
        agg = _aggregate_lines(lines)
        assert (
            agg["abs_variance"]
            == agg["positive_variance"] + agg["negative_variance_abs"]
        )


# ---------------------------------------------------------------------------
# _combine_aggregates
# ---------------------------------------------------------------------------


class TestCombineAggregates:
    def test_empty_list_returns_zeros_with_count_zero(self) -> None:
        totals = _combine_aggregates([])
        assert totals == {
            "stock_take_count": 0,
            "lines_with_variance": 0,
            "positive_variance": Decimal("0"),
            "negative_variance_abs": Decimal("0"),
            "net_variance": Decimal("0"),
            "abs_variance": Decimal("0"),
        }

    def test_sum_across_two_rows(self) -> None:
        rows = [
            {
                "aggregate": {
                    "lines_with_variance": 1,
                    "positive_variance": Decimal("3"),
                    "negative_variance_abs": Decimal("0"),
                    "net_variance": Decimal("3"),
                    "abs_variance": Decimal("3"),
                }
            },
            {
                "aggregate": {
                    "lines_with_variance": 2,
                    "positive_variance": Decimal("0"),
                    "negative_variance_abs": Decimal("5"),
                    "net_variance": Decimal("-5"),
                    "abs_variance": Decimal("5"),
                }
            },
        ]
        totals = _combine_aggregates(rows)
        assert totals["stock_take_count"] == 2
        assert totals["lines_with_variance"] == 3
        assert totals["positive_variance"] == Decimal("3")
        assert totals["negative_variance_abs"] == Decimal("5")
        assert totals["net_variance"] == Decimal("-2")
        assert totals["abs_variance"] == Decimal("8")
