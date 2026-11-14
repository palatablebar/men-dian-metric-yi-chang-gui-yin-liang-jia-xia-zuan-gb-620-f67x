"""异常服务：列表 / 详情 / 复核 / 序列化。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    ANOMALY_SCOPE_LABELS,
    CHANNEL_LABELS,
    DIRECTION_LABELS,
    METRIC_LABELS,
    REVIEW_STATUS_LABELS,
    SEVERITY_LABELS,
    STORE_SCOPE_CHANNEL,
)
from src.models.anomaly import Anomaly
from src.models.attribution import Attribution
from src.models.store import Store
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, parse_date, utcnow


def _channel_label(channel: str) -> str | None:
    if channel == STORE_SCOPE_CHANNEL:
        return None
    return CHANNEL_LABELS.get(channel, channel)


def serialize(
    anomaly: Anomaly,
    store: Store | None = None,
    attribution_id: int | None = None,
    parent: Anomaly | None = None,
    child_count: int = 0,
) -> dict:
    return {
        "id": anomaly.id,
        "run_id": anomaly.run_id,
        "store_id": anomaly.store_id,
        "store_code": store.code if store else None,
        "store_name": store.name if store else None,
        "region": store.region if store else None,
        "city": store.city if store else None,
        "metric_date": anomaly.metric_date.isoformat(),
        "metric_key": anomaly.metric_key,
        "metric_label": METRIC_LABELS.get(anomaly.metric_key, anomaly.metric_key),
        "channel": anomaly.channel,
        "channel_label": _channel_label(anomaly.channel),
        "scope": anomaly.scope,
        "scope_label": ANOMALY_SCOPE_LABELS.get(anomaly.scope, anomaly.scope),
        "parent_id": anomaly.parent_id,
        "parent_attribution_id": None,
        "suppressed": bool(anomaly.suppressed),
        "child_count": child_count,
        "observed": round(anomaly.observed, 2),
        "baseline": round(anomaly.baseline, 2),
        "mad": round(anomaly.mad, 2),
        "robust_z": round(anomaly.robust_z, 4),
        "direction": anomaly.direction,
        "direction_label": DIRECTION_LABELS.get(anomaly.direction, anomaly.direction),
        "severity": anomaly.severity,
        "severity_label": SEVERITY_LABELS.get(anomaly.severity, anomaly.severity),
        "impact_amount": round(anomaly.impact_amount, 2),
        "status": anomaly.status,
        "status_label": REVIEW_STATUS_LABELS.get(anomaly.status, anomaly.status),
        "reviewer": anomaly.reviewer,
        "review_note": anomaly.review_note,
        "reviewed_at": iso_utc(anomaly.reviewed_at),
        "attribution_id": attribution_id,
        "payload": anomaly.payload or {},
        "created_at": iso_utc(anomaly.created_at),
        "updated_at": iso_utc(anomaly.updated_at),
        # 父异常（门店总量）信息，供页面直接展示「对应门店波动」
        "parent": (
            {
                "id": parent.id,
                "metric_date": parent.metric_date.isoformat(),
                "robust_z": round(parent.robust_z, 4),
                "impact_amount": round(parent.impact_amount, 2),
                "severity": parent.severity,
            }
            if parent
            else None
        ),
    }


def _attribution_map(db: Session) -> dict[int, int]:
    rows = db.execute(select(Attribution.anomaly_id, Attribution.id)).all()
    return {anomaly_id: attribution_id for anomaly_id, attribution_id in rows}


def list_anomalies(
    db: Session,
    *,
    store_id: int | None = None,
    region: str | None = None,
    metric_key: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    channel: str | None = None,
    suppressed: bool | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    q: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    stmt = select(Anomaly)
    count_stmt = select(func.count()).select_from(Anomaly)
    conditions = []
    if store_id:
        conditions.append(Anomaly.store_id == store_id)
    if metric_key:
        conditions.append(Anomaly.metric_key == metric_key)
    if severity:
        conditions.append(Anomaly.severity == severity)
    if status:
        conditions.append(Anomaly.status == status)
    if scope:
        conditions.append(Anomaly.scope == scope)
    if channel:
        conditions.append(Anomaly.channel == channel)
    if suppressed is not None:
        conditions.append(Anomaly.suppressed == suppressed)
    parsed_from = parse_date(date_from)
    parsed_to = parse_date(date_to)
    if parsed_from:
        conditions.append(Anomaly.metric_date >= parsed_from)
    if parsed_to:
        conditions.append(Anomaly.metric_date <= parsed_to)
    for condition in conditions:
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)
    if region or q:
        store_stmt = select(Store.id)
        if region:
            store_stmt = store_stmt.where(Store.region == region)
        if q:
            like = f"%{q.strip()}%"
            store_stmt = store_stmt.where(Store.name.ilike(like) | Store.code.ilike(like))
        store_ids = [row for row in db.scalars(store_stmt).all()]
        if not store_ids:
            return {"items": [], "total": 0, "limit": limit, "offset": offset}
        stmt = stmt.where(Anomaly.store_id.in_(store_ids))
        count_stmt = count_stmt.where(Anomaly.store_id.in_(store_ids))
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(
        stmt.order_by(
            Anomaly.suppressed.asc(),
            Anomaly.metric_date.desc(),
            func.abs(Anomaly.robust_z).desc(),
        ).limit(limit).offset(offset)
    ).all()
    stores = {store.id: store for store in db.scalars(select(Store)).all()}
    attribution_ids = _attribution_map(db)
    parent_ids = {row.parent_id for row in rows if row.parent_id}
    parents = {item.id: item for item in db.scalars(select(Anomaly).where(Anomaly.id.in_(parent_ids))).all()} if parent_ids else {}
    child_counts = dict(
        db.execute(
            select(Anomaly.parent_id, func.count())
            .where(Anomaly.parent_id.is_not(None))
            .group_by(Anomaly.parent_id)
        ).all()
    )
    return {
        "items": [
            serialize(
                row,
                stores.get(row.store_id),
                attribution_ids.get(row.id),
                parents.get(row.parent_id) if row.parent_id else None,
                child_counts.get(row.id, 0),
            )
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_anomaly(db: Session, anomaly_id: int) -> Anomaly:
    anomaly = db.get(Anomaly, anomaly_id)
    if anomaly is None:
        raise not_found("anomaly", anomaly_id)
    return anomaly


def get_with_context(db: Session, anomaly_id: int) -> dict:
    anomaly = get_anomaly(db, anomaly_id)
    store = db.get(Store, anomaly.store_id)
    attribution_ids = _attribution_map(db)
    parent = db.get(Anomaly, anomaly.parent_id) if anomaly.parent_id else None
    child_count = db.scalar(
        select(func.count()).select_from(Anomaly).where(Anomaly.parent_id == anomaly.id)
    ) or 0
    data = serialize(anomaly, store, attribution_ids.get(anomaly.id), parent, child_count)
    if parent is not None:
        data["parent_attribution_id"] = attribution_ids.get(parent.id)
    return data


def review(db: Session, anomaly: Anomaly, *, status: str, note: str | None, reviewer: str) -> Anomaly:
    anomaly.status = status
    anomaly.review_note = note
    anomaly.reviewer = reviewer
    anomaly.reviewed_at = utcnow()
    anomaly.updated_at = utcnow()
    db.flush()
    return anomaly


def count_by(db: Session, column) -> list[tuple[str, int]]:
    return db.execute(select(column, func.count()).group_by(column)).all()


def ensure_anomaly(db: Session, payload: dict) -> tuple[Anomaly, bool]:
    """自然键 (store_id, metric_date, metric_key, channel) 幂等 upsert；不覆盖复核状态。

    - 已存在且仍为 pending：用最新检测结果刷新算法字段（含 parent/suppressed 关联）；
    - 已存在且已人工复核（confirmed/dismissed）：只回补关联字段，绝不动结论与算法结论；
    - 不存在：插入新行。
    """
    existing = db.scalar(
        select(Anomaly).where(
            Anomaly.store_id == payload["store_id"],
            Anomaly.metric_date == payload["metric_date"],
            Anomaly.metric_key == payload["metric_key"],
            Anomaly.channel == payload.get("channel", STORE_SCOPE_CHANNEL),
        )
    )
    relation_fields = ("parent_id", "suppressed", "scope", "channel")
    if existing is not None:
        if existing.status == "pending":
            existing.run_id = payload.get("run_id") or existing.run_id
            existing.observed = payload["observed"]
            existing.baseline = payload["baseline"]
            existing.mad = payload["mad"]
            existing.robust_z = payload["robust_z"]
            existing.severity = payload["severity"]
            existing.direction = payload["direction"]
            existing.impact_amount = payload["impact_amount"]
            existing.payload = payload["payload"]
            for field_name in relation_fields:
                if field_name in payload:
                    setattr(existing, field_name, payload[field_name])
        else:
            # 人工复核完成：只允许补关联，不覆盖任何复核/算法结论
            for field_name in relation_fields:
                if field_name in payload and getattr(existing, field_name) in (None, "", False):
                    setattr(existing, field_name, payload[field_name])
        db.flush()
        return existing, False
    anomaly = Anomaly(**payload)
    db.add(anomaly)
    db.flush()
    return anomaly, True


def mark_reviewed(db: Session, anomaly: Anomaly, reviewer: str, note: str) -> None:
    if anomaly.status != "pending":
        return
    if not reviewer:
        raise AppError("VALIDATION", 422, {"field": "reviewer"})
    anomaly.status = "confirmed" if anomaly.severity in ("high", "medium") else "dismissed"
    anomaly.reviewer = reviewer
    anomaly.review_note = note
    anomaly.reviewed_at = utcnow()
    db.flush()
