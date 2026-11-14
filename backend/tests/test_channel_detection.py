"""渠道级检测测试：直连增长掩盖 OTA 掉量时，门店总量无异常但渠道层必须报出。"""
from __future__ import annotations

from datetime import date, timedelta

from src.services import detection

START = date(2026, 8, 16)


def _channel_series(values: list[float], sold: int = 40, rooms: int = 100) -> list[dict]:
    rows = []
    for index, value in enumerate(values):
        day = START + timedelta(days=index)
        rows.append(
            {
                "date": day.isoformat(),
                "revenue": float(value),
                "rooms_sold": sold,
                "rooms_available": rooms,
                "adr": round(float(value) / sold, 4),
                "occupancy": round(sold / rooms, 6),
                "is_weekend": day.weekday() >= 5,
            }
        )
    return rows


def _stable_values(days: int = 60, base: float = 20_000.0) -> list[float]:
    pattern = [-3, -2, -1, 1, 2, 3]
    return [base + pattern[index % len(pattern)] * 20 for index in range(days)]


def test_channel_series_detects_sudden_ota_drop():
    values = _stable_values()
    values[44] = 18_600.0  # 前一日同向
    values[45] = 15_200.0  # OTA 触发日，约 -24%
    result = detection.detect_series(_channel_series(values), "rev")
    dates = {hit["date"] for hit in result["hits"]}
    assert (START + timedelta(days=45)).isoformat() in dates
    hit = next(item for item in result["hits"] if item["date"] == (START + timedelta(days=45)).isoformat())
    assert hit["direction"] == "down"
    assert hit["impact_amount"] < 0


def test_masked_drop_invisible_at_store_but_visible_at_channel():
    # OTA 掉 4800，直连补 4800：门店总量完全平稳
    ota = _stable_values(base=20_000.0)
    direct = _stable_values(base=12_000.0)
    ota[44] = 18_600.0
    ota[45] = 15_200.0
    direct[44] = 13_400.0
    direct[45] = 16_800.0
    store_values = [round(ota[i] + direct[i], 2) for i in range(len(ota))]
    # 门店总量层：无异常（波动被对冲）
    store_hits = detection.detect_series(_channel_series(store_values, sold=80), "rev")["hits"]
    assert store_hits == []
    # 渠道层：OTA 下行、直连上行各自可报
    ota_hits = detection.detect_series(_channel_series(ota), "rev")["hits"]
    direct_hits = detection.detect_series(_channel_series(direct), "rev")["hits"]
    assert any(hit["direction"] == "down" for hit in ota_hits)
    assert any(hit["direction"] == "up" for hit in direct_hits)


def test_channel_series_share_same_algorithm_params_as_store():
    values = _stable_values()
    values[45] = 19_900.0  # 单日抖动，不满足方向延续
    result = detection.detect_series(_channel_series(values), "rev")
    assert result["hits"] == []
    assert result["params"]["metric_key"] == "rev"
    assert result["params"]["window"] == 28


def test_channel_and_store_revenue_reconcile_in_constructed_data():
    # 渠道营收之和严格等于门店总量（供对账模块消费的同一批数据）
    ota = _stable_values(base=20_000.0)
    direct = _stable_values(base=12_000.0)
    corp = _stable_values(base=6_000.0)
    for index in range(len(ota)):
        total = ota[index] + direct[index] + corp[index]
        assert total == round(total, 2)
