from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class Login(BaseModel):
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=100)


class Import(BaseModel):
    name: str = Field(min_length=1, max_length=180)
    csv: str = Field(min_length=1, max_length=1_000_000)


class Run(BaseModel):
    dataset_id: int = Field(gt=0)
    options: dict = Field(default_factory=dict)


class Review(BaseModel):
    decision: Literal['approved', 'returned']
    note: str = Field(min_length=2, max_length=1000)


class Delivery(BaseModel):
    recipient: str = Field(min_length=2, max_length=100)
    cadence: Literal['once', 'daily']
    scheduled_at: datetime | None = None


class Supplement(BaseModel):
    name: str | None = Field(default='迟到补传', max_length=180)
    csv: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    dataset_id: int | None = Field(default=None, gt=0)
    target_month: str | None = None
    month: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=120)


class LockPeriod(BaseModel):
    month: str | None = None
    report_month: str | None = None
    run_id: int | None = Field(default=None, gt=0)
    dataset_id: int | None = Field(default=None, gt=0)
