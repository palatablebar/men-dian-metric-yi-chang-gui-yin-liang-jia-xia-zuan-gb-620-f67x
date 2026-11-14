"""同波动去重与门店↔渠道关联规则测试（直连掩盖 OTA 掉量的核心场景）。"""
from __future__ import annotations

from src.services import suppression


def _store_hit(store_id=1, day="2026-11-03", direction="down"):
    return {"store_id": store_id, "date": day, "metric_key": "rev", "direction": direction}


def _channel_hit(channel, store_id=1, day="2026-11-03", direction="down"):
    return {
        "store_id": store_id,
        "date": day,
        "metric_key": "rev",
        "direction": direction,
        "channel": channel,
    }


def test_same_direction_channel_hit_is_suppressed_and_linked_to_store():
    store_hits = suppression.mark_store_hits([_store_hit(direction="down")])
    channel_hits = [_channel_hit("ota", direction="down")]
    linked = suppression.link_channel_anomalies(store_hits, channel_hits)
    assert len(linked) == 1
    hit = linked[0]
    assert hit["suppressed"] is True
    assert hit["scope"] == "channel"
    assert hit["parent_key"] == {"store_id": 1, "date": "2026-11-03", "metric_key": "rev", "channel": ""}


def test_opposite_direction_channel_hit_kept_visible_but_still_linked():
    # 门店下行，但直连逆势上行：不抑制，独立可见，仍关联门店异常
    store_hits = suppression.mark_store_hits([_store_hit(direction="down")])
    channel_hits = [_channel_hit("direct", direction="up")]
    linked = suppression.link_channel_anomalies(store_hits, channel_hits)
    assert linked[0]["suppressed"] is False
    assert linked[0]["parent_key"] is not None


def test_channel_only_anomaly_visible_when_store_total_is_flat():
    # 核心诉求：门店总量平稳（直连增长抵消 OTA 掉量），OTA 渠道异常必须独立可见
    linked = suppression.link_channel_anomalies(store_hits=[], channel_hits=[_channel_hit("ota")])
    assert len(linked) == 1
    assert linked[0]["suppressed"] is False
    assert linked[0]["parent_key"] is None
    assert linked[0]["scope"] == "channel"


def test_multiple_channels_one_suppressed_one_independent():
    store_hits = suppression.mark_store_hits([_store_hit(direction="down")])
    channel_hits = [
        _channel_hit("ota", direction="down"),
        _channel_hit("direct", direction="up"),
        _channel_hit("corp", direction="down"),
    ]
    linked = suppression.link_channel_anomalies(store_hits, channel_hits)
    by_channel = {item["channel"]: item for item in linked}
    assert by_channel["ota"]["suppressed"] is True
    assert by_channel["corp"]["suppressed"] is True
    assert by_channel["direct"]["suppressed"] is False


def test_mark_store_hits_uses_sentinel_channel():
    marked = suppression.mark_store_hits([_store_hit()])
    assert marked[0]["scope"] == "store"
    assert marked[0]["channel"] == ""
    assert marked[0]["suppressed"] is False


def test_dedupe_pairs_keeps_first_per_natural_key():
    pairs = [
        _channel_hit("ota"),
        _channel_hit("ota"),
        _channel_hit("direct"),
    ]
    unique = suppression.dedupe_pairs(pairs)
    assert len(unique) == 2
    assert {(item["store_id"], item["date"], item["metric_key"], item["channel"]) for item in unique} == {
        (1, "2026-11-03", "rev", "ota"),
        (1, "2026-11-03", "rev", "direct"),
    }


def test_inputs_are_not_mutated():
    store_hits = suppression.mark_store_hits([_store_hit()])
    original = _channel_hit("ota")
    snapshot = dict(original)
    suppression.link_channel_anomalies(store_hits, [original])
    assert original == snapshot
