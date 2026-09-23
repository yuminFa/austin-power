"""Tests for scripts/measure_rss.py (spec U8: startup vs. after-1000-calls RSS budget).

Loaded via importlib like contrib/agentmemory_to_jsonl.py's test, since it's a
standalone script rather than a package module (see test_contrib_agentmemory.py).
"""

import importlib.util
import subprocess
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "measure_rss", Path(__file__).parents[1] / "scripts" / "measure_rss.py"
)
measure_rss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(measure_rss)

pytestmark = pytest.mark.kiwi


def test_growth_pct_basic():
    assert measure_rss.growth_pct(100, 120) == pytest.approx(20.0)
    assert measure_rss.growth_pct(50, 50) == 0.0


def test_growth_pct_rejects_nonpositive_before():
    with pytest.raises(ValueError):
        measure_rss.growth_pct(0, 10)


def test_read_rss_mb_reports_positive_value_for_live_process():
    proc = subprocess.Popen(["sleep", "5"])
    try:
        rss = measure_rss.read_rss_mb(proc.pid)
        assert rss > 0
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_run_measurement_end_to_end(tmp_path):
    # Small iteration count so the suite stays fast; the real 1,000-call
    # measurement for the README is run separately (plan Task 9 Step 2).
    result = measure_rss.run_measurement(iterations=20, home=tmp_path / "h")
    assert set(result) == {
        "startup_rss_mb",
        "after_first_call_rss_mb",
        "after_1000_rss_mb",
        "growth_pct",
    }
    assert result["startup_rss_mb"] > 0
    assert result["after_first_call_rss_mb"] > 0
    assert result["after_1000_rss_mb"] > 0
    assert isinstance(result["growth_pct"], float)
