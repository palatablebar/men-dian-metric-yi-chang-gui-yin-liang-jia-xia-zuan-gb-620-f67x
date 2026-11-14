from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ReviewIn(BaseModel):
    status: Literal["pending", "confirmed", "dismissed"]
    note: str | None = Field(default=None, max_length=1000)


class AnomalyQuery(BaseModel):
    store_id: int | None = None
    metric_key: str | None = Field(default=None, pattern=r"^(rev|adr|occ)$")
    severity: str | None = Field(default=None, pattern=r"^(high|medium|low)$")
    status: str | None = Field(default=None, pattern=r"^(pending|confirmed|dismissed)$")
    scope: str | None = Field(default=None, pattern=r"^(store|channel)$")
    channel: str | None = Field(default=None, pattern=r"^(ota|direct|corp)$")
    suppressed: bool | None = None
    date_from: str | None = None
    date_to: str | None = None
