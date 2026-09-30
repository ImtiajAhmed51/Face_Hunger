"""Latency budgets on synthetic libraries. Slow: run with `pytest -m perf`."""

import pytest

pytestmark = pytest.mark.perf


def test_hybrid_search_p95_under_300ms_at_100k():
    from scripts.bench_search import bench

    result = bench(100_000, rounds=10)
    assert result["p95_ms"] < 300, result
