"""检测运行集成测试（SQLite 内存库）：双层检测落库、同波动抑制、失败续跑、复核保护、对账。

不依赖外部 MySQL：用 SQLite create_all 直接建出新结构（schema_sync 对非 MySQL 跳过 ALTER）。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from src.constants.enums import STORE_SCOPE_CHANNEL
from src.models import Base
from src.models.anomaly import Anomaly
from src.models.attribution import Attribution
from src.models.metric import MetricPoint
from src.models.run import Run
from src.models.store import Store
from src.services import anomaly_service, run_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    seed_store_and_points(session)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _add_point(session, store_id, day, channel, *, sold, adr, rooms=100, weekend=None):
    weekend = day.weekday() >= 5 if weekend is None else weekend
    session.add(
        MetricPoint(
            store_id=store_id,
            stat_date=day,
            channel=channel,
            rooms_available=rooms,
            rooms_sold=sold,
            occupancy=round(sold / rooms, 6),
            adr=adr,
            revenue=round(adr * sold, 2),
            is_weekend=weekend,
            room_type_mix={},
        )
    )


def seed_store_and_points(session) -> None:
    """门店 1：门店总营收每日恒定（门店层退化基线、不产异常），波动只在渠道间对冲。

    - 正常日三渠道营收仅做 ±小抖动，门店合计恒定 → 门店 MAD=0 被跳过；
    - dip/target 日 OTA 掉量由直连等额补齐 → 门店总量依然平稳；
    - OTA 渠道自身基线有波动（MAD>0），连续两日同向下跌触发渠道异常。
    """
    store = Store(
        code="LB-T-001", name="测试店", region="华东", city="上海",
        brand="蓝湾精选", room_count=100, manager="t", status="open",
    )
    session.add(store)
    session.flush()
    start = date(2026, 8, 16)
    days = 60
    pattern = [-3, -2, -1, 1, 2, 3]
    target = start + timedelta(days=45)
    dip = start + timedelta(days=44)
    channel_base = {"ota": (44, 525.0), "direct": (24, 495.0), "corp": (12, 420.0)}
    for offset in range(days):
        day = start + timedelta(days=offset)
        k = pattern[offset % len(pattern)]
        revenues = {
            "ota": 44 * 525.0 + k * 20.0,
            "direct": 24 * 495.0 - k * 20.0,  # 与 OTA 反向抖动，门店合计恒定
            "corp": 12 * 420.0,
        }
        if day == dip:
            revenues["ota"] -= 400.0
            revenues["direct"] += 400.0
        if day == target:
            revenues["ota"] -= 1_400.0
            revenues["direct"] += 1_400.0
        for channel, (sold_base, adr) in channel_base.items():
            revenue = revenues[channel]
            sold = sold_base
            session.add(
                MetricPoint(
                    store_id=store.id,
                    stat_date=day,
                    channel=channel,
                    rooms_available=100,
                    rooms_sold=sold,
                    occupancy=round(sold / 100, 6),
                    adr=round(revenue / sold, 4),
                    revenue=round(revenue, 2),
                    is_weekend=day.weekday() >= 5,
                    room_type_mix={},
                )
            )
    session.flush()


def test_channel_anomaly_persisted_when_store_total_is_flat(db):
    run = run_service.create_and_execute(
        db, store_id=None, trigger="manual", operator_email="admin@x.io",
        params={"metrics": ["rev"], "detect_channels": True},
    )
    db.commit()
    assert run.status in ("success", "partial")
    channel_anomalies = db.scalars(select(Anomaly).where(Anomaly.scope == "channel")).all()
    target = date(2026, 8, 16) + timedelta(days=45)
    ota = [a for a in channel_anomalies if a.channel == "ota" and a.metric_date == target]
    assert len(ota) == 1
    assert ota[0].direction == "down"
    # 门店总量被对冲，门店层不应在该日产生异常
    store_hits = db.scalars(
        select(Anomaly).where(Anomaly.scope == "store", Anomaly.metric_date == target)
    ).all()
    assert store_hits == []
    # 独立渠道异常不被抑制
    assert ota[0].suppressed is False
    assert ota[0].parent_id is None


def test_same_wave_channel_anomaly_suppressed_and_linked(db):
    # 再造一家门店：门店总量本身异常，OTA 同向异常应被抑制并挂 parent
    store2 = Store(
        code="LB-T-002", name="测试店2", region="华东", city="杭州",
        brand="蓝湾轻居", room_count=100, manager="t2", status="open",
    )
    db.add(store2)
    db.flush()
    start = date(2026, 8, 16)
    pattern = [-3, -2, -1, 1, 2, 3]
    target = start + timedelta(days=45)
    dip = start + timedelta(days=44)
    for offset in range(60):
        day = start + timedelta(days=offset)
        k = pattern[offset % len(pattern)]
        ota_sold, direct_sold, corp_sold = 44, 24, 12
        if day == dip:
            ota_sold, direct_sold, corp_sold = 30, 22, 11
        if day == target:
            # 全渠道齐跌、且没有对冲 → 门店总量与 OTA 同向异常
            ota_sold, direct_sold, corp_sold = 18, 12, 6
        _add_point(db, store2.id, day, "ota", sold=ota_sold, adr=525.0 + k)
        _add_point(db, store2.id, day, "direct", sold=direct_sold, adr=495.0 + k)
        _add_point(db, store2.id, day, "corp", sold=corp_sold, adr=420.0 + k)
    db.flush()
    run = run_service.create_and_execute(
        db, store_id=None, trigger="manual", operator_email="admin@x.io",
    )
    db.commit()
    assert run.status in ("success", "partial")
    store_hit = db.scalar(
        select(Anomaly).where(Anomaly.store_id == store2.id, Anomaly.scope == "store", Anomaly.metric_date == target)
    )
    assert store_hit is not None
    ota_hit = db.scalar(
        select(Anomaly).where(
            Anomaly.store_id == store2.id, Anomaly.scope == "channel",
            Anomaly.channel == "ota", Anomaly.metric_date == target,
        )
    )
    assert ota_hit is not None
    assert ota_hit.suppressed is True
    assert ota_hit.parent_id == store_hit.id
    # 抑制项不计入 run.anomaly_count 的独立异常口径
    visible_count = db.scalar(select(func.count()).select_from(Anomaly).where(Anomaly.suppressed.is_(False)))
    assert run.anomaly_count == visible_count


def test_reconciliation_report_balanced_on_consistent_seed(db):
    run = run_service.create_and_execute(
        db, store_id=None, trigger="manual", operator_email="admin@x.io",
    )
    db.commit()
    report = run.metrics["reconciliation"]
    assert report["status"] == "balanced"
    assert report["failure_count"] == 0


def test_natural_key_idempotent_on_rerun(db):
    first = run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    count_after_first = db.scalar(select(func.count()).select_from(Anomaly))
    second = run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    count_after_second = db.scalar(select(func.count()).select_from(Anomaly))
    assert count_after_first == count_after_second
    assert first.id != second.id


def test_rerun_does_not_overwrite_human_review(db):
    run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    anomaly = db.scalars(select(Anomaly).where(Anomaly.suppressed.is_(False))).first()
    original_observed = anomaly.observed
    anomaly_service.review(db, anomaly, status="confirmed", note="人工已确认", reviewer="analyst@x.io")
    db.commit()
    # 重跑
    run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    refreshed = db.get(Anomaly, anomaly.id)
    assert refreshed.status == "confirmed"
    assert refreshed.review_note == "人工已确认"
    assert refreshed.reviewer == "analyst@x.io"
    assert refreshed.observed == original_observed


def test_failed_run_keeps_completed_store_and_resume_finishes_rest(db, monkeypatch):
    # 加第二家门店，让第一家处理完后在第二家注入失败
    store2 = Store(
        code="LB-T-002", name="测试店2", region="华南", city="深圳",
        brand="蓝湾度假", room_count=100, manager="t2", status="open",
    )
    db.add(store2)
    db.flush()
    start = date(2026, 8, 16)
    for offset in range(60):
        day = start + timedelta(days=offset)
        _add_point(db, store2.id, day, "ota", sold=40, adr=500.0)
        _add_point(db, store2.id, day, "direct", sold=24, adr=480.0)
        _add_point(db, store2.id, day, "corp", sold=12, adr=420.0)
    db.flush()

    real_process = run_service._process_store
    call_count = {"n": 0}

    def flaky_process(session, run, store, *args, **kwargs):
        call_count["n"] += 1
        if store.code == "LB-T-002":
            raise RuntimeError("注入的单店故障")
        return real_process(session, run, store, *args, **kwargs)

    monkeypatch.setattr(run_service, "_process_store", flaky_process)
    failed = run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    assert failed.status == "failed"
    completed = failed.checkpoint["completed_stores"]
    store1 = db.scalar(select(Store).where(Store.code == "LB-T-001"))
    assert store1.id in completed
    # 第一家门店的异常已落库，未因第二家失败而丢失
    assert db.scalar(select(func.count()).select_from(Anomaly).where(Anomaly.store_id == store1.id)) > 0

    # 恢复真实处理逻辑后续跑
    monkeypatch.setattr(run_service, "_process_store", real_process)
    continuation = run_service.resume_failed_run(db, failed, operator_email="a@x.io")
    db.commit()
    assert continuation.recovered_from_id == failed.id
    assert continuation.status in ("success", "partial")
    refreshed_failed = db.get(Run, failed.id)
    assert refreshed_failed.status == "recovered"
    # 续跑只处理未完成门店：检查点继承，最终覆盖全部门店
    all_store_ids = set(db.scalars(select(Store.id)).all())
    assert set(continuation.checkpoint["completed_stores"]) == all_store_ids


def test_first_store_failure_keeps_run_row_and_resume_recovers(db, monkeypatch):
    """第一家门店在首次提交前失败：批次行必须仍在库、状态 failed、可续跑。"""
    real_process = run_service._process_store
    call = {"n": 0}

    def always_fail(session, run, store, *args, **kwargs):
        call["n"] += 1
        if call["n"] == 1:
            raise RuntimeError("首店即故障")
        return real_process(session, run, store, *args, **kwargs)

    monkeypatch.setattr(run_service, "_process_store", always_fail)
    failed = run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    # 批次行没有因回滚而消失
    persisted = db.get(Run, failed.id)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.checkpoint.get("failed_stores")

    monkeypatch.setattr(run_service, "_process_store", real_process)
    continuation = run_service.resume_failed_run(db, failed, operator_email="a@x.io")
    db.commit()
    assert continuation.status in ("success", "partial")
    assert db.get(Run, failed.id).status == "recovered"


def test_resume_rejects_non_failed_run(db):
    from src.utils.errors import AppError

    run = run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    if run.status == "success":
        with pytest.raises(AppError) as exc_info:
            run_service.resume_failed_run(db, run, operator_email="a@x.io")
        assert exc_info.value.status_code == 422


def test_channel_independent_anomaly_gets_attribution(db):
    run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    target = date(2026, 8, 16) + timedelta(days=45)
    ota = db.scalar(
        select(Anomaly).where(Anomaly.scope == "channel", Anomaly.channel == "ota", Anomaly.metric_date == target)
    )
    assert ota is not None
    attribution = db.scalar(select(Attribution).where(Attribution.anomaly_id == ota.id))
    assert attribution is not None
    # LMDI 闭合：三项 + 残差 ≈ Δ营收
    total = attribution.adr_part + attribution.occ_part + attribution.rooms_part + attribution.residual
    assert abs(total - attribution.total_delta) < 1.0


def test_channel_natural_key_includes_channel_sentinel_for_store_scope(db):
    run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    store_rows = db.scalars(select(Anomaly).where(Anomaly.scope == "store")).all()
    assert all(row.channel == STORE_SCOPE_CHANNEL for row in store_rows)
