"""对账纯函数测试：量价恒等式、渠道汇总、LMDI 闭合、不平留痕。"""
from __future__ import annotations

from src.services import reconcile


def test_identity_balanced_when_revenue_equals_adr_times_sold():
    row = {"adr": 500.0, "occupancy": 0.8, "rooms_available": 100, "rooms_sold": 80, "revenue": 40_000.0}
    result = reconcile.check_identity(row)
    assert result.ok is True
    assert result.detail["gap"] == 0.0


def test_identity_detects_rounding_gap_within_tolerance():
    row = {"adr": 500.0, "occupancy": 0.8, "rooms_available": 100, "rooms_sold": 80, "revenue": 40_000.6}
    result = reconcile.check_identity(row)
    assert result.ok is True  # 0.6 元在 1 元容差内


def test_identity_flags_mismatch_beyond_tolerance():
    row = {"adr": 500.0, "occupancy": 0.8, "rooms_available": 100, "rooms_sold": 80, "revenue": 38_000.0}
    result = reconcile.check_identity(row)
    assert result.ok is False
    assert "不平" in result.message
    assert result.detail["gap"] == -2_000.0


def test_channel_sum_balances_to_store_total():
    store = {"revenue": 10_000.0, "rooms_sold": 20}
    channels = [
        {"channel": "ota", "revenue": 5_500.0, "rooms_sold": 11},
        {"channel": "direct", "revenue": 3_000.0, "rooms_sold": 6},
        {"channel": "corp", "revenue": 1_500.0, "rooms_sold": 3},
    ]
    result = reconcile.check_channel_sum(store, channels)
    assert result.ok is True
    assert result.detail["revenue_gap"] == 0.0
    assert result.detail["sold_gap"] == 0


def test_channel_sum_detects_missing_channel_revenue():
    store = {"revenue": 10_000.0, "rooms_sold": 20}
    channels = [
        {"channel": "ota", "revenue": 4_000.0, "rooms_sold": 8},
        {"channel": "direct", "revenue": 3_000.0, "rooms_sold": 7},
        {"channel": "corp", "revenue": 1_500.0, "rooms_sold": 5},
    ]
    result = reconcile.check_channel_sum(store, channels)
    assert result.ok is False
    assert result.detail["revenue_gap"] == 1_500.0
    assert result.detail["sold_gap"] == 0


def test_channel_sum_sold_mismatch_is_strict_zero_tolerance():
    store = {"revenue": 10_000.0, "rooms_sold": 20}
    channels = [
        {"channel": "ota", "revenue": 5_500.0, "rooms_sold": 10},
        {"channel": "direct", "revenue": 3_000.0, "rooms_sold": 6},
        {"channel": "corp", "revenue": 1_500.0, "rooms_sold": 3},
    ]
    result = reconcile.check_channel_sum(store, channels)
    assert result.ok is False
    assert result.detail["sold_gap"] == 1


def test_decompose_closed_when_residual_absorbs_float_gap():
    parts = {"adr": -620.0, "occ": -310.0, "rooms": -70.0}
    result = reconcile.check_decompose_closed(parts, residual=0.0001, total_delta=-1000.0)
    assert result.ok is True
    assert abs(result.detail["gap"]) <= 1.0


def test_decompose_closed_flags_broken_identity():
    parts = {"adr": -620.0, "occ": -310.0, "rooms": -70.0}
    result = reconcile.check_decompose_closed(parts, residual=0.0, total_delta=-900.0)
    assert result.ok is False


def test_reconcile_store_day_runs_all_checks():
    store = {"adr": 500.0, "occupancy": 0.8, "rooms_available": 100, "rooms_sold": 80, "revenue": 40_000.0}
    channels = [
        {"channel": "ota", "adr": 525.0, "occupancy": 0.44, "rooms_available": 100, "rooms_sold": 44, "revenue": 23_100.0},
        {"channel": "direct", "adr": 495.0, "occupancy": 0.24, "rooms_available": 100, "rooms_sold": 24, "revenue": 11_880.0},
        {"channel": "corp", "adr": round(5020.0 / 12, 4), "occupancy": 0.12, "rooms_available": 100, "rooms_sold": 12, "revenue": 5_020.0},
    ]
    results = reconcile.reconcile_store_day(store, channels)
    codes = {result.code for result in results}
    assert codes == {"identity", "channel_sum"}
    assert all(result.ok for result in results)


def test_build_report_marks_mismatch_and_keeps_every_failure():
    good = reconcile.CheckResult("identity", True, {"x": 1})
    bad_one = reconcile.CheckResult("channel_sum", False, {"x": 2}, "不平 A")
    bad_two = reconcile.CheckResult("identity", False, {"x": 3}, "不平 B")
    report = reconcile.build_report([good, bad_one, bad_two])
    assert report.status == "mismatch"
    assert report.as_dict()["failure_count"] == 2
    assert {item["message"] for item in report.failures} == {"不平 A", "不平 B"}


def test_build_report_balanced_when_all_ok():
    report = reconcile.build_report([
        reconcile.CheckResult("identity", True, {}),
        reconcile.CheckResult("channel_sum", True, {}),
    ])
    assert report.status == "balanced"
    assert report.as_dict()["failure_count"] == 0
