"""运行时幂等结构升级（轻量迁移，替代手工 ALTER）。

背景：`Base.metadata.create_all` 只建不存在的表，不会给已存在的表加列/改约束。
本模块在应用启动建表之后执行，逐条检查 information_schema，已存在则跳过，
因此可重复执行、对全新库与旧库都安全；全部 DDL 用 IF EXISTS / 检查兜住方言差异。

升级项（渠道级检测 + 失败续跑）：
- anomalies：新增 channel/scope/parent_id/suppressed，唯一键由
  (store_id, metric_date, metric_key) 扩为 (..., channel)；
- runs：新增 checkpoint / recovered_from_id。
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.utils.logger import get_logger

logger = get_logger("schema_sync")

_ANOMALY_COLUMNS: dict[str, str] = {
    "channel": "VARCHAR(20) NOT NULL DEFAULT ''",
    "scope": "VARCHAR(12) NOT NULL DEFAULT 'store'",
    "parent_id": "BIGINT NULL",
    "suppressed": "TINYINT(1) NOT NULL DEFAULT 0",
}

_RUN_COLUMNS: dict[str, str] = {
    "checkpoint": "JSON NULL",
    "recovered_from_id": "BIGINT NULL",
}


def _existing_columns(conn, table_name: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :table"
        ),
        {"table": table_name},
    ).all()
    return {row[0] for row in rows}


def _table_exists(conn, table_name: str) -> bool:
    row = conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :table"
        ),
        {"table": table_name},
    ).first()
    return row is not None


def _existing_indexes(conn, table_name: str) -> set[str]:
    rows = conn.execute(
        text(
            "SELECT DISTINCT index_name FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = :table"
        ),
        {"table": table_name},
    ).all()
    return {row[0] for row in rows}


def sync_schema(engine: Engine) -> list[str]:
    """执行幂等升级，返回实际执行的 DDL 描述列表（供日志/测试断言）。"""
    applied: list[str] = []
    if engine.dialect.name != "mysql":
        # 非 MySQL（如测试用 SQLite）依赖 create_all 直接建出新结构，无需 ALTER。
        logger.info("skip runtime ALTER on dialect=%s (create_all handles fresh schema)", engine.dialect.name)
        return applied

    with engine.begin() as conn:
        if _table_exists(conn, "anomalies"):
            existing = _existing_columns(conn, "anomalies")
            for column, ddl in _ANOMALY_COLUMNS.items():
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE anomalies ADD COLUMN {column} {ddl}"))
                    applied.append(f"anomalies.+{column}")
            indexes = _existing_indexes(conn, "anomalies")
            if "uq_anomaly_store_date_metric" in indexes:
                conn.execute(text("ALTER TABLE anomalies DROP INDEX uq_anomaly_store_date_metric"))
                applied.append("anomalies.-uq_anomaly_store_date_metric")
            if "uq_anomaly_store_date_metric_channel" not in indexes:
                conn.execute(
                    text(
                        "ALTER TABLE anomalies ADD CONSTRAINT uq_anomaly_store_date_metric_channel "
                        "UNIQUE (store_id, metric_date, metric_key, channel)"
                    )
                )
                applied.append("anomalies.+uq_anomaly_store_date_metric_channel")
            if "ix_anomalies_channel" not in indexes:
                conn.execute(text("ALTER TABLE anomalies ADD INDEX ix_anomalies_channel (channel)"))
                applied.append("anomalies.+ix_anomalies_channel")
            if "ix_anomalies_scope" not in indexes:
                conn.execute(text("ALTER TABLE anomalies ADD INDEX ix_anomalies_scope (scope)"))
                applied.append("anomalies.+ix_anomalies_scope")
            if "ix_anomalies_parent_id" not in indexes:
                conn.execute(text("ALTER TABLE anomalies ADD INDEX ix_anomalies_parent_id (parent_id)"))
                applied.append("anomalies.+ix_anomalies_parent_id")
            if "ix_anomalies_suppressed" not in indexes:
                conn.execute(text("ALTER TABLE anomalies ADD INDEX ix_anomalies_suppressed (suppressed)"))
                applied.append("anomalies.+ix_anomalies_suppressed")

        if _table_exists(conn, "runs"):
            existing = _existing_columns(conn, "runs")
            for column, ddl in _RUN_COLUMNS.items():
                if column not in existing:
                    conn.execute(text(f"ALTER TABLE runs ADD COLUMN {column} {ddl}"))
                    applied.append(f"runs.+{column}")
            indexes = _existing_indexes(conn, "runs")
            if "ix_runs_recovered_from_id" not in indexes:
                conn.execute(text("ALTER TABLE runs ADD INDEX ix_runs_recovered_from_id (recovered_from_id)"))
                applied.append("runs.+ix_runs_recovered_from_id")

    if applied:
        logger.info("schema sync applied: %s", ", ".join(applied))
    return applied
