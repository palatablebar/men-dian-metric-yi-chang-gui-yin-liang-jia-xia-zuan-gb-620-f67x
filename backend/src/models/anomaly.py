"""异常（门店/渠道 × 日期 × 指标 唯一），由算法 A 产出。

自然键：(store_id, metric_date, metric_key, channel)
- 门店总量异常 channel = ""（STORE_SCOPE_CHANNEL 哨兵）；
- 渠道异常 channel ∈ {ota, direct, corp}，与 MetricPoint.channel 同口径。

关联与去重：
- scope=store / channel；
- 渠道异常被同门店同日期的门店异常解释时，parent_id 指向门店异常且 suppressed=true，
  不重复计数、不重复推送，但保留明细供下钻；
- 门店总量平稳、仅单渠道异常时，渠道异常 suppressed=false 独立可见（直连增长掩盖 OTA 掉量场景）。
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class Anomaly(Base):
    __tablename__ = "anomalies"
    __table_args__ = (
        UniqueConstraint(
            "store_id",
            "metric_date",
            "metric_key",
            "channel",
            name="uq_anomaly_store_date_metric_channel",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    metric_date: Mapped[date] = mapped_column(Date, index=True)
    metric_key: Mapped[str] = mapped_column(String(12), default="rev")
    # 门店总量异常为 ""；渠道异常为 ota/direct/corp
    channel: Mapped[str] = mapped_column(String(20), default="", index=True)
    scope: Mapped[str] = mapped_column(String(12), default="store", index=True)
    # 渠道异常关联到的同门店同日期门店总量异常（无则为空，表示渠道独立异常）
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("anomalies.id", ondelete="SET NULL"), nullable=True, index=True
    )
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    observed: Mapped[float] = mapped_column(Float, default=0.0)
    baseline: Mapped[float] = mapped_column(Float, default=0.0)
    mad: Mapped[float] = mapped_column(Float, default=0.0)
    robust_z: Mapped[float] = mapped_column(Float, default=0.0)
    direction: Mapped[str] = mapped_column(String(8), default="down")
    severity: Mapped[str] = mapped_column(String(12), default="medium", index=True)
    impact_amount: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reviewer: Mapped[str | None] = mapped_column(String(160), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
