"""跨版本共享的模型约定（骨架）。"""
from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str
