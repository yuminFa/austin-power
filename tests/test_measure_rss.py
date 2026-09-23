"""Tests for scripts/measure_rss.py.

Covers the original spec U8 (startup vs. after-1000-calls RSS budget) and its
2.5.1 re-measurement after the Kiwi worker split: idle server RSS, active
server+worker RSS, first-call latency after an idle worker unload, and
rebuild-1000-notes latency after a server restart.

Loaded via importlib like contrib/agentmemory_to_jsonl.py's test, since it's a
standalone script rather than a package module (see test_contrib_agentmemory.py).
"""

import importlib.util
import os
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


# -- 2.5.1 re-measurement: process helpers -----------------------------------


def test_find_child_pids_finds_spawned_child():
    proc = subprocess.Popen(["sleep", "5"])
    try:
        kids = measure_rss.find_child_pids(os.getpid())
        assert proc.pid in kids
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_find_child_pids_empty_for_childless_pid():
    proc = subprocess.Popen(["sleep", "5"])
    try:
        # sleep itself (almost certainly) has no children of its own.
        assert measure_rss.find_child_pids(proc.pid) == []
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_pid_alive_true_then_false_after_exit():
    proc = subprocess.Popen(["sleep", "1"])
    assert measure_rss.pid_alive(proc.pid) is True
    proc.wait(timeout=5)
    assert measure_rss.pid_alive(proc.pid) is False


def test_read_total_rss_mb_sums_multiple_live_pids():
    a = subprocess.Popen(["sleep", "5"])
    b = subprocess.Popen(["sleep", "5"])
    try:
        total = measure_rss.read_total_rss_mb([a.pid, b.pid])
        one = measure_rss.read_rss_mb(a.pid)
        assert total > one
    finally:
        for p in (a, b):
            p.terminate()
            p.wait(timeout=5)


def test_read_total_rss_mb_skips_dead_pids():
    proc = subprocess.Popen(["sleep", "1"])
    proc.wait(timeout=5)
    # A pid that has already exited contributes 0, not an error.
    assert measure_rss.read_total_rss_mb([proc.pid]) == 0


# -- 2.5.1 re-measurement: acceptance thresholds (pure, no subprocess) -------


def test_check_acceptance_all_pass_within_budget():
    results = {
        "idle_rss_mb": 90.0,
        "active_total_rss_mb": 500.0,
        "first_call_after_idle_s": 1.5,
        "rebuild_1000_s": 10.0,
    }
    acceptance = measure_rss.check_acceptance(results)
    assert all(acceptance.values())


def test_check_acceptance_detects_each_failure_independently():
    base = {
        "idle_rss_mb": 90.0,
        "active_total_rss_mb": 500.0,
        "first_call_after_idle_s": 1.5,
        "rebuild_1000_s": 10.0,
    }
    over_idle = {**base, "idle_rss_mb": 200.0}
    assert measure_rss.check_acceptance(over_idle)["idle_rss_mb<=120"] is False

    over_active = {**base, "active_total_rss_mb": 700.0}
    assert measure_rss.check_acceptance(over_active)["active_total_rss_mb<=650"] is False

    over_first_call = {**base, "first_call_after_idle_s": 3.5}
    assert measure_rss.check_acceptance(over_first_call)["first_call_after_idle_s<=3"] is False

    over_rebuild = {**base, "rebuild_1000_s": 16.0}
    assert measure_rss.check_acceptance(over_rebuild)["rebuild_1000_s<=15"] is False


def test_check_acceptance_boundary_values_pass():
    # spec says "<=" — exactly-at-threshold must pass, not fail.
    results = {
        "idle_rss_mb": 120.0,
        "active_total_rss_mb": 650.0,
        "first_call_after_idle_s": 3.0,
        "rebuild_1000_s": 15.0,
    }
    assert all(measure_rss.check_acceptance(results).values())


# -- 2.5.1 re-measurement: real subprocess flows (small sizes for speed) -----


def test_run_worker_memory_measurement_smoke(tmp_path):
    # Small iteration count so the suite stays fast; the real 1,000-call
    # measurement for the README/spec is run separately.
    result = measure_rss.run_worker_memory_measurement(iterations=4, home=tmp_path / "h")
    assert set(result) == {"idle_rss_mb", "active_total_rss_mb"}
    assert result["idle_rss_mb"] > 0
    # The worker adds Kiwi's resident memory on top of the idle server.
    assert result["active_total_rss_mb"] > result["idle_rss_mb"]


def test_run_idle_reload_measurement_smoke(tmp_path):
    result = measure_rss.run_idle_reload_measurement(
        home=tmp_path / "h", idle_seconds=1, settle_s=2.5
    )
    assert set(result) == {"worker_gone_after_idle", "first_call_after_idle_s"}
    assert result["worker_gone_after_idle"] is True
    assert result["first_call_after_idle_s"] > 0


def test_run_rebuild_measurement_smoke(tmp_path):
    result = measure_rss.run_rebuild_measurement(notes=5, home=tmp_path / "h")
    assert set(result) == {"rebuild_1000_s"}
    assert result["rebuild_1000_s"] > 0


def test_check_acceptance_matches_main_exit_code():
    # main() must exit 1 if any acceptance item fails, 0 if all pass — verified
    # here against check_acceptance directly rather than a full real run
    # (that's exercised once, separately, for the actual README/spec numbers).
    passing = {
        "idle_rss_mb": 1.0,
        "active_total_rss_mb": 1.0,
        "first_call_after_idle_s": 0.1,
        "rebuild_1000_s": 0.1,
        "growth_pct": 0.0,
    }
    assert all(measure_rss.check_acceptance(passing).values())
    failing = {**passing, "rebuild_1000_s": 999.0}
    assert not all(measure_rss.check_acceptance(failing).values())
