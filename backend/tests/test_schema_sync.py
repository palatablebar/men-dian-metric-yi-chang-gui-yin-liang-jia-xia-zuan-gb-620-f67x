"""运行时结构升级：非 MySQL 方言安全跳过（全新库由 create_all 建出新结构）。"""
from __future__ import annotations

from sqlalchemy import create_engine

from src.config.schema_sync import sync_schema


def test_sync_schema_skips_sqlite_without_error():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    assert sync_schema(engine) == []


def test_sync_schema_is_idempotent_on_sqlite():
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    first = sync_schema(engine)
    second = sync_schema(engine)
    assert first == [] and second == []
