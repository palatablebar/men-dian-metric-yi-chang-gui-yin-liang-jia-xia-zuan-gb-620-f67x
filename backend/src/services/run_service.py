"""检测运行服务：门店总量 + 渠道双层检测 → 关联去重 → 对账 → 落异常 → 归因。

可靠性口径（与 README §10 同步）：
- 按门店处理、按门店提交：单店失败只回滚该店，已完成门店的结果已落库，不发生数据丢失；
- 检查点 run.checkpoint 记录 completed/failed 门店；批次整体失败时旧结果全部保留；
- 失败批次可经 resume_failed_run 续跑：新批次只处理未完成门店，已完成门店（含人工复核）不重算；
- 自然键幂等 upsert（anomaly_service.ensure_anomaly），重跑不覆盖人工复核结论。

检测口径：
- 门店层与渠道层复用同一套算法 A（detection.detect_series），渠道序列由
  metric_service.channel_series_points 按门店聚合口径产出；
- 同波动去重（suppression.link_channel_anomalies）：渠道异常关联门店异常，
  同向抑制、反向保留；门店平稳时渠道异常独立可见；
- 对账（reconcile）：量价恒等式、渠道明细 vs 门店总量逐日对平，不平则批次 partial。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import CHANNEL_ORDER, METRIC_LABELS, STORE_SCOPE_CHANNEL
from src.models.anomaly import Anomaly
from src.models.metric import MetricPoint
from src.models.run import Run
from src.models.store import Store
from src.services import (
    anomaly_service,
    attribution_service,
    detection,
    metric_service,
    reconcile,
    suppression,
)
from src.utils.errors import AppError
from src.utils.timeutil import iso_utc, local_now, utcnow

DEFAULT_PARAMS = {
    "metrics": ["rev"],
    "window": 28,
    "threshold": 3.5,
    "persist_z": 1.5,
    "stratify": True,
    "detect_channels": True,
}


def serialize(run: Run, store_name: str | None = None) -> dict:
    return {
        "id": run.id,
        "run_no": run.run_no,
        "store_id": run.store_id,
        "store_name": store_name,
        "trigger": run.trigger,
        "status": run.status,
        "operator_email": run.operator_email,
        "store_count": run.store_count,
        "point_count": run.point_count,
        "anomaly_count": run.anomaly_count,
        "params": run.params or {},
        "metrics": run.metrics or {},
        "log_text": run.log_text,
        "checkpoint": run.checkpoint or {},
        "recovered_from_id": run.recovered_from_id,
        "started_at": iso_utc(run.started_at),
        "finished_at": iso_utc(run.finished_at),
    }


def next_run_no(db: Session) -> str:
    prefix = f"DT{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(Run).where(Run.run_no.like(f"{prefix}%"))) or 0
    return f"{prefix}-{count + 1:04d}"


def _normalize_params(payload: dict | None) -> dict:
    params = dict(DEFAULT_PARAMS)
    for key, value in (payload or {}).items():
        if value is None:
            continue
        params[key] = value
    metrics = [item for item in params.get("metrics") or ["rev"] if item in METRIC_LABELS]
    params["metrics"] = metrics or ["rev"]
    params["detect_channels"] = bool(params.get("detect_channels", True))
    return params


def _hit_payload(store_id: int, hit: dict, metric_key: str, run_id: int, *, channel: str,
                 scope: str, suppressed: bool, parent_id: int | None, extra: dict | None = None) -> dict:
    payload = {
        "store_id": store_id,
        "metric_date": date.fromisoformat(hit["date"]),
        "metric_key": metric_key,
        "channel": channel,
        "scope": scope,
        "suppressed": suppressed,
        "parent_id": parent_id,
        "observed": hit["observed"],
        "baseline": hit["baseline"],
        "mad": hit["mad"],
        "robust_z": hit["robust_z"],
        "direction": hit["direction"],
        "severity": hit["severity"],
        "impact_amount": hit["impact_amount"],
        "run_id": run_id,
        "status": "pending",
        "payload": {
            "window_start": hit["window_start"],
            "window_end": hit["window_end"],
            "window_samples": hit["window_samples"],
            "mad_basis": hit["mad_basis"],
            "baseline_triple": hit["baseline_triple"],
            "current_triple": hit["current_triple"],
            "params": hit.get("params", {}),
        },
    }
    if extra:
        payload["payload"].update(extra)
    return payload


def _detect_options(params: dict) -> dict:
    return {
        "window": params["window"],
        "threshold": params["threshold"],
        "persist_z": params["persist_z"],
        "stratify": params["stratify"],
    }


def _process_store(
    db: Session,
    run: Run,
    store: Store,
    store_series: list[dict],
    channel_series: dict[str, list[dict]],
    params: dict,
    counters: dict,
) -> None:
    """单门店双层检测 + 关联去重 + 落库 + 归因，在调用方事务内执行。"""
    options = _detect_options(params)
    detect_channels = bool(params.get("detect_channels", True))

    for metric_key in params["metrics"]:
        store_result = detection.detect_series(store_series, metric_key, **options)
        for reason in store_result["skipped"]:
            counters["skipped_reasons"][reason["reason"]] = (
                counters["skipped_reasons"].get(reason["reason"], 0) + 1
            )
        store_hits = suppression.mark_store_hits(store_result["hits"])

        # 先落门店异常，拿到主键供渠道异常挂 parent
        store_anomaly_ids: dict[tuple, int] = {}
        for hit in store_hits:
            hit["store_id"] = store.id
            payload = _hit_payload(
                store.id, hit, metric_key, run.id,
                channel=STORE_SCOPE_CHANNEL, scope="store", suppressed=False, parent_id=None,
            )
            anomaly, is_new = anomaly_service.ensure_anomaly(db, payload)
            counters["created"] += 1 if is_new else 0
            counters["updated"] += 0 if is_new else 1
            counters["hit_total"] += 1
            counters["by_severity"][anomaly.severity] = counters["by_severity"].get(anomaly.severity, 0) + 1
            counters["by_metric"][metric_key] = counters["by_metric"].get(metric_key, 0) + 1
            counters["z_values"].append(abs(anomaly.robust_z))
            store_anomaly_ids[(store.id, hit["date"], metric_key)] = anomaly.id
            attribution_service.build(db, anomaly, store)

        # 渠道层检测
        raw_channel_hits: list[dict] = []
        channel_skipped = 0
        if detect_channels:
            for channel in CHANNEL_ORDER:
                series = channel_series.get(channel)
                if not series:
                    continue
                channel_result = detection.detect_series(series, metric_key, **options)
                channel_skipped += len(channel_result["skipped"])
                for hit in channel_result["hits"]:
                    hit["store_id"] = store.id
                    hit["channel"] = channel
                    raw_channel_hits.append(hit)
            for _ in range(channel_skipped):
                counters["skipped_reasons"]["channel_skip"] = (
                    counters["skipped_reasons"].get("channel_skip", 0) + 1
                )

        linked = suppression.link_channel_anomalies(store_hits, raw_channel_hits)
        linked = suppression.dedupe_pairs(linked)
        for hit in linked:
            parent_key = hit.get("parent_key")
            parent_id = store_anomaly_ids.get(
                (store.id, hit["date"], metric_key)
            ) if parent_key else None
            payload = _hit_payload(
                store.id, hit, metric_key, run.id,
                channel=hit["channel"], scope="channel",
                suppressed=bool(hit["suppressed"]), parent_id=parent_id,
                extra={"channel": hit["channel"]},
            )
            anomaly, is_new = anomaly_service.ensure_anomaly(db, payload)
            if hit["suppressed"]:
                counters["suppressed_total"] += 1
                continue
            counters["created"] += 1 if is_new else 0
            counters["updated"] += 0 if is_new else 1
            counters["hit_total"] += 1
            counters["by_severity"][anomaly.severity] = counters["by_severity"].get(anomaly.severity, 0) + 1
            key = f"{metric_key}:{hit['channel']}"
            counters["by_metric"][key] = counters["by_metric"].get(key, 0) + 1
            counters["z_values"].append(abs(anomaly.robust_z))
            # 渠道独立异常（门店平稳）同样产出量价结论，页面可直接下钻
            if parent_id is None:
                attribution_service.build(db, anomaly, store)


def _reconcile_store(db: Session, store_id: int, start: date, end: date) -> list[reconcile.CheckResult]:
    """对门店区间内每一天做量价恒等式 + 渠道汇总对账。"""
    days = db.scalars(
        select(MetricPoint.stat_date)
        .where(MetricPoint.store_id == store_id, MetricPoint.stat_date >= start, MetricPoint.stat_date <= end)
        .group_by(MetricPoint.stat_date)
    ).all()
    results: list[reconcile.CheckResult] = []
    for day in days:
        channel_rows = metric_service.store_day_channels(db, store_id, day)
        if not channel_rows:
            continue
        revenue = round(sum(float(row.revenue) for row in channel_rows), 2)
        sold = int(sum(int(row.rooms_sold) for row in channel_rows))
        rooms = max(int(row.rooms_available) for row in channel_rows)
        store_row = {
            "revenue": revenue,
            "rooms_sold": sold,
            "rooms_available": rooms,
            "adr": round(revenue / sold, 6) if sold else 0.0,
            "occupancy": round(sold / rooms, 6) if rooms else 0.0,
        }
        channel_dicts = [
            {
                "channel": row.channel,
                "revenue": float(row.revenue),
                "rooms_sold": row.rooms_sold,
                "rooms_available": row.rooms_available,
                "adr": float(row.adr),
                "occupancy": float(row.occupancy),
            }
            for row in channel_rows
        ]
        for result in reconcile.reconcile_store_day(store_row, channel_dicts):
            result.detail = {"store_id": store_id, "date": day.isoformat(), **result.detail}
            results.append(result)
    return results


def execute(db: Session, run: Run) -> Run:
    run.status = "running"
    params = _normalize_params(run.params)
    run.params = params
    # 先把批次行持久化：即使第一家门店就在首次提交前失败，run 记录与检查点依然在，
    # 失败后可凭该批次续跑，不会因回滚丢失运行痕迹。
    if not run.checkpoint:
        run.checkpoint = {"completed_stores": [], "failed_stores": []}
    db.commit()
    # 续跑场景：只处理原失败批次未完成的门店
    completed_stores = list((run.checkpoint or {}).get("completed_stores", []))
    try:
        stores = db.scalars(select(Store).order_by(Store.code)).all()
        if run.store_id:
            stores = [store for store in stores if store.id == run.store_id]
        bounds = metric_service.date_bounds(db)
        grouped = metric_service.series_points(db, bounds[0], bounds[1], run.store_id)
        channel_grouped = (
            metric_service.channel_series_points(db, bounds[0], bounds[1], run.store_id)
            if params.get("detect_channels", True)
            else {}
        )
        point_count = db.scalar(select(func.count()).select_from(MetricPoint)) or 0

        counters = {
            "created": 0,
            "updated": 0,
            "hit_total": 0,
            "suppressed_total": 0,
            "by_severity": {"high": 0, "medium": 0, "low": 0},
            "by_metric": {},
            "skipped_reasons": {},
            "z_values": [],
        }
        failed_stores: list[int] = []
        reconciliation_results: list[reconcile.CheckResult] = []

        for store in stores:
            if store.id in completed_stores:
                continue
            try:
                series = grouped.get(store.id)
                if not series:
                    # 无数据的门店也记为完成，避免续跑空转
                    completed_stores.append(store.id)
                    continue
                channel_series = {
                    channel: channel_grouped.get((store.id, channel), []) for channel in CHANNEL_ORDER
                }
                _process_store(db, run, store, series, channel_series, params, counters)
                reconciliation_results.extend(_reconcile_store(db, store.id, bounds[0], bounds[1]))
                completed_stores.append(store.id)
                # 按门店提交：单店失败只回滚该店，已落库结果不丢
                run.checkpoint = {
                    "completed_stores": completed_stores,
                    "failed_stores": failed_stores,
                }
                db.commit()
            except Exception as exc:  # noqa: BLE001 - 单店隔离，续跑可补
                db.rollback()
                failed_stores.append(store.id)
                run = db.get(Run, run.id) or run
                run.checkpoint = {
                    "completed_stores": completed_stores,
                    "failed_stores": failed_stores,
                    "last_error": f"store {store.id}: {exc}"[:300],
                }
                db.commit()

        reconciliation_report = reconcile.build_report(reconciliation_results).as_dict()
        impact_total = db.scalar(
            select(func.sum(Anomaly.impact_amount))
            .where(Anomaly.run_id == run.id, Anomaly.suppressed.is_(False))
        ) or 0.0
        run = db.get(Run, run.id) or run
        run.store_count = len(stores)
        run.point_count = point_count
        run.anomaly_count = counters["hit_total"]
        run.metrics = {
            "metrics": params["metrics"],
            "detect_channels": params.get("detect_channels", True),
            "window": params["window"],
            "threshold": params["threshold"],
            "persist_z": params["persist_z"],
            "stores": len(stores),
            "completed_stores": len(completed_stores),
            "failed_stores": len(failed_stores),
            "points": point_count,
            "anomalies": counters["hit_total"],
            "suppressed": counters["suppressed_total"],
            "created": counters["created"],
            "updated": counters["updated"],
            "severity": counters["by_severity"],
            "by_metric": counters["by_metric"],
            "impact_total": round(float(impact_total), 2),
            "max_abs_z": round(max(counters["z_values"]), 4) if counters["z_values"] else 0.0,
            "skipped": counters["skipped_reasons"],
            "reconciliation": reconciliation_report,
        }
        if failed_stores:
            run.status = "failed"
        elif reconciliation_report["status"] == "mismatch":
            run.status = "partial"
        else:
            run.status = "success" if counters["created"] or counters["updated"] else "partial"
        run.checkpoint = {
            "completed_stores": completed_stores,
            "failed_stores": failed_stores,
            "reconciliation": {
                "status": reconciliation_report["status"],
                "failure_count": reconciliation_report["failure_count"],
            },
        }
        run.log_text = (
            f"检测 {len(stores)} 家门店（完成 {len(completed_stores)}、失败 {len(failed_stores)}）"
            f" / {point_count} 条指标点；命中 {counters['hit_total']} 起独立异常"
            f"（渠道关联抑制 {counters['suppressed_total']} 起；新增 {counters['created']}、更新 {counters['updated']}）；"
            f"high={counters['by_severity'].get('high', 0)} medium={counters['by_severity'].get('medium', 0)} "
            f"low={counters['by_severity'].get('low', 0)}；对账 {reconciliation_report['status']}"
            f"（{reconciliation_report['failure_count']} 条不平）；影响合计 {float(impact_total):,.0f} 元。"
            f"口径：window={params['window']} threshold={params['threshold']} "
            f"persist_z={params['persist_z']} 周末分层={params['stratify']} 渠道检测={params.get('detect_channels', True)}"
        )
    except Exception as exc:  # noqa: BLE001 - 运行失败要留痕，不抛 500；已提交门店结果保留
        db.rollback()
        run = db.get(Run, run.id) or run
        run.status = "failed"
        run.log_text = f"检测失败（已完成门店结果已保留，可续跑）：{exc}"
        run.metrics = {"error": str(exc)[:300], "completed_stores": len(completed_stores)}
        if not run.checkpoint:
            run.checkpoint = {"completed_stores": completed_stores, "failed_stores": []}
    run.finished_at = utcnow()
    db.flush()
    return run


def create_and_execute(
    db: Session,
    *,
    store_id: int | None,
    trigger: str,
    operator_email: str,
    params: dict | None = None,
    seed_key: str | None = None,
) -> Run:
    if seed_key:
        existing = db.scalar(select(Run).where(Run.seed_key == seed_key))
        if existing is not None:
            return existing
    if store_id and db.get(Store, store_id) is None:
        raise AppError("NOT_FOUND", 404, {"entity": "store", "id": store_id})
    run = Run(
        run_no=next_run_no(db),
        store_id=store_id,
        trigger=trigger,
        status="running",
        operator_email=operator_email,
        params=_normalize_params(params),
        seed_key=seed_key,
        checkpoint={"completed_stores": [], "failed_stores": []},
    )
    db.add(run)
    db.flush()
    return execute(db, run)


def resume_failed_run(db: Session, failed_run: Run, *, operator_email: str) -> Run:
    """对失败/部分批次续跑：只处理未完成门店，原批次与已复核结论原样保留。"""
    if failed_run.status not in ("failed", "partial"):
        raise AppError(
            "VALIDATION",
            422,
            {"run_id": failed_run.id, "status": failed_run.status, "reason": "仅失败或部分成功的批次可续跑"},
        )
    checkpoint = failed_run.checkpoint or {}
    completed = list(checkpoint.get("completed_stores", []))
    continuation = Run(
        run_no=next_run_no(db),
        store_id=failed_run.store_id,
        trigger="manual",
        status="running",
        operator_email=operator_email,
        params=_normalize_params(failed_run.params),
        recovered_from_id=failed_run.id,
        checkpoint={"completed_stores": completed, "failed_stores": [], "resumed_from": failed_run.run_no},
    )
    db.add(continuation)
    db.flush()
    executed = execute(db, continuation)
    # 原批次标记为已被续跑（保留记录与日志，不删除、不覆盖）
    if executed.status in ("success", "partial"):
        failed_run.status = "recovered"
        db.flush()
    return executed


def list_runs(db: Session, *, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    stmt = select(Run)
    count_stmt = select(func.count()).select_from(Run)
    if status:
        stmt = stmt.where(Run.status == status)
        count_stmt = count_stmt.where(Run.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(Run.id.desc()).limit(limit).offset(offset)).all()
    stores = {store.id: store.name for store in db.scalars(select(Store)).all()}
    return {
        "items": [serialize(row, stores.get(row.store_id)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


def get_run(db: Session, run_id: int) -> Run:
    run = db.get(Run, run_id)
    if run is None:
        raise AppError("NOT_FOUND", 404, {"entity": "run", "id": run_id})
    return run
