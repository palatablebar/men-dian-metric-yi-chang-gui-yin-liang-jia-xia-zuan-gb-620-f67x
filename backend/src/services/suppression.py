"""同波动去重与门店↔渠道关联（纯函数、可单测）。

背景：一次 OTA 掉量同时会在「门店总量」和「OTA 渠道」两层触发检测，若各算一条会重复报警。
规则（同一 (store_id, metric_date) 内裁决）：
1. 若存在门店总量异常（scope=store），同方向的渠道异常视为该门店波动的渠道解释：
   parent_id 指向门店异常、suppressed=true，保留明细用于下钻，但不计数、不推送；
   反方向的渠道异常（如门店下行但某渠道逆势上行）不抑制，独立可见，parent 仍可关联；
2. 若门店总量平稳、没有门店异常（直连增长掩盖 OTA 掉量的典型场景），
   渠道异常 suppressed=false、parent_id=None，作为独立异常进入清单；
3. 门店异常之间按自然键幂等，渠道异常之间按 (store,date,metric,channel) 幂等，
   本模块只做层间裁决，不产生重复行。

输入/输出均为普通 dict，不触碰 ORM，便于确定性测试。
"""
from __future__ import annotations

from src.constants.enums import STORE_SCOPE_CHANNEL


def link_channel_anomalies(store_hits: list[dict], channel_hits: list[dict]) -> list[dict]:
    """给渠道命中补齐 parent_key / suppressed / scope 字段。

    store_hits / channel_hits 元素至少含 store_id、date、metric_key、direction；
    渠道命中额外含 channel。返回追加了裁决字段的新列表（不改入参）。
    """
    store_index: dict[tuple, dict] = {
        (item["store_id"], item["date"], item["metric_key"]): item for item in store_hits
    }
    linked: list[dict] = []
    for hit in channel_hits:
        item = dict(hit)
        item["scope"] = "channel"
        item.setdefault("channel", item.get("channel", ""))
        parent = store_index.get((item["store_id"], item["date"], item["metric_key"]))
        if parent is not None:
            item["parent_key"] = {
                "store_id": parent["store_id"],
                "date": parent["date"],
                "metric_key": parent["metric_key"],
                "channel": STORE_SCOPE_CHANNEL,
            }
            item["suppressed"] = parent["direction"] == item["direction"]
        else:
            item["parent_key"] = None
            item["suppressed"] = False
        linked.append(item)
    return linked


def mark_store_hits(store_hits: list[dict]) -> list[dict]:
    """门店命中统一打 scope=store、不抑制。"""
    marked: list[dict] = []
    for hit in store_hits:
        item = dict(hit)
        item["scope"] = "store"
        item["channel"] = STORE_SCOPE_CHANNEL
        item["suppressed"] = False
        item["parent_key"] = None
        marked.append(item)
    return marked


def dedupe_pairs(linked_pairs: list[dict]) -> list[dict]:
    """按渠道自然键去重（同一渠道同门店同日同指标只保留一条，确定性取首条）。"""
    seen: set[tuple] = set()
    unique: list[dict] = []
    for item in linked_pairs:
        key = (item["store_id"], item["date"], item["metric_key"], item["channel"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique
