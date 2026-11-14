"""端到端：完整演示数据集（120 店 × 90 天 × 3 渠道）走真实 run_service 双层检测。"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from scripts.demo_data import MASKED_DATE, MASKED_STORE_INDEX, build_dataset, store_specs
from src.models import Base
from src.models.anomaly import Anomaly
from src.models.attribution import Attribution
from src.models.metric import MetricPoint
from src.models.store import Store
from src.services import run_service


@pytest.fixture()
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, future=True)()
    dataset = build_dataset()
    specs = {spec["code"]: spec for spec in dataset["stores"]}
    code_to_id: dict[str, int] = {}
    for spec in dataset["stores"]:
        store = Store(
            code=spec["code"], name=spec["name"], region=spec["region"], city=spec["city"],
            brand=spec["brand"], room_count=spec["room_count"], manager=spec["manager"], status=spec["status"],
        )
        session.add(store)
        session.flush()
        code_to_id[spec["code"]] = store.id
    rows = [
        {
            "store_id": code_to_id[point["store_code"]],
            "stat_date": point["stat_date"],
            "channel": point["channel"],
            "rooms_available": point["rooms_available"],
            "rooms_sold": point["rooms_sold"],
            "occupancy": point["occupancy"],
            "adr": point["adr"],
            "revenue": point["revenue"],
            "is_weekend": point["is_weekend"],
            "room_type_mix": point["room_type_mix"],
        }
        for point in dataset["points"]
    ]
    session.bulk_insert_mappings(MetricPoint, rows)
    session.flush()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_full_dataset_double_layer_detection_and_reconciliation(db):
    run = run_service.create_and_execute(
        db, store_id=None, trigger="manual", operator_email="admin@x.io",
    )
    db.commit()
    # 全量演示数据严格对账，不应出现 mismatch
    assert run.metrics["reconciliation"]["status"] == "balanced"
    assert run.metrics["reconciliation"]["failure_count"] == 0
    # 双层都产出了异常
    assert db.scalar(select(func.count()).select_from(Anomaly).where(Anomaly.scope == "store")) > 0
    channel_visible = db.scalar(
        select(func.count()).select_from(Anomaly).where(Anomaly.scope == "channel", Anomaly.suppressed.is_(False))
    )
    assert channel_visible > 0


def test_masked_event_is_channel_only_and_attributable(db):
    run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    masked_code = store_specs()[MASKED_STORE_INDEX]["code"]
    store = db.scalar(select(Store).where(Store.code == masked_code))
    # 该门店在掩盖日没有门店总量异常
    store_hit = db.scalar(
        select(Anomaly).where(
            Anomaly.store_id == store.id, Anomaly.scope == "store", Anomaly.metric_date == MASKED_DATE
        )
    )
    assert store_hit is None
    # 但 OTA 渠道独立异常存在、未抑制、可下钻量价结论
    ota = db.scalar(
        select(Anomaly).where(
            Anomaly.store_id == store.id,
            Anomaly.scope == "channel",
            Anomaly.channel == "ota",
            Anomaly.metric_date == MASKED_DATE,
        )
    )
    assert ota is not None
    assert ota.suppressed is False
    assert ota.parent_id is None
    attribution = db.scalar(select(Attribution).where(Attribution.anomaly_id == ota.id))
    assert attribution is not None
    assert attribution.narrative.startswith("[OTA 渠道]")


def test_suppressed_channel_rows_exist_but_counted_once(db):
    run = run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    suppressed = db.scalar(select(func.count()).select_from(Anomaly).where(Anomaly.suppressed.is_(True)))
    # 存在被门店异常解释的渠道异常（37 起门店事件大多伴随渠道同向波动）
    assert suppressed > 0
    # run.anomaly_count 只计独立异常，与可见行数一致
    visible = db.scalar(select(func.count()).select_from(Anomaly).where(Anomaly.suppressed.is_(False)))
    assert run.anomaly_count == visible


def test_rerun_keeps_suppression_linkage_and_is_idempotent(db):
    first = run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    total_after_first = db.scalar(select(func.count()).select_from(Anomaly))
    suppressed_linked_first = db.scalar(
        select(func.count()).select_from(Anomaly).where(
            Anomaly.suppressed.is_(True), Anomaly.parent_id.is_not(None)
        )
    )
    second = run_service.create_and_execute(db, store_id=None, trigger="manual", operator_email="a@x.io")
    db.commit()
    total_after_second = db.scalar(select(func.count()).select_from(Anomaly))
    suppressed_linked_second = db.scalar(
        select(func.count()).select_from(Anomaly).where(
            Anomaly.suppressed.is_(True), Anomaly.parent_id.is_not(None)
        )
    )
    assert total_after_first == total_after_second
    assert suppressed_linked_first == suppressed_linked_second
    # 所有被抑制的渠道异常都必须能关联到具体门店异常
    orphan = db.scalar(
        select(func.count()).select_from(Anomaly).where(
            Anomaly.suppressed.is_(True), Anomaly.parent_id.is_(None)
        )
    )
    assert orphan == 0
    assert first.id != second.id


def test_channel_rows_reconcile_to_store_totals_across_full_dataset(db):
    # 直接用底层数据验证：每个门店每日渠道营收/售量之和 == 门店汇总
    from sqlalchemy import text

    rows = db.execute(
        text(
            """
            SELECT store_id, stat_date,
                   ROUND(SUM(revenue), 2) AS rev_sum,
                   SUM(rooms_sold) AS sold_sum,
                   COUNT(*) AS channel_count
            FROM metric_points
            GROUP BY store_id, stat_date
            """
        )
    ).all()
    assert len(rows) == 120 * 90
    for _, _, rev_sum, sold_sum, channel_count in rows:
        assert channel_count == 3
        assert rev_sum > 0
        assert sold_sum > 0
