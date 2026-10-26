"""Request and result contract shared by the worker, the runners and the MCP tool."""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Status(StrEnum):
    CART_READY = "CART_READY"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    AMBIGUOUS_MATCH = "AMBIGUOUS_MATCH"
    VARIANT_UNAVAILABLE = "VARIANT_UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    FAILED = "FAILED"


class CartRequest(BaseModel):
    product_name: str
    options: dict[str, str] = Field(default_factory=dict)
    quantity: int = Field(default=1, ge=1, le=10)
    recovery: bool = True

    @field_validator("product_name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("product_name must not be empty")
        return v


class Price(BaseModel):
    amount: str
    currency: str


class CartLine(BaseModel):
    product_name: str
    product_url: str
    options: dict[str, str] = Field(default_factory=dict)
    defaulted: list[str] = Field(default_factory=list)
    quantity: int
    observed_price: Price | None = None
    verified: bool


class Result(BaseModel):
    status: Status
    message: str
    cart: CartLine | None = None
    candidates: list[str] = Field(default_factory=list)
    checkout_submitted: Literal[False] = False
    recovery_used: bool = False
    model_calls: int = 0
    recovery_trace: list[str] = Field(default_factory=list)
    failed_step: str | None = None
    runner: str = "local"
    recipe_version: str | None = None
    duration_ms: int = 0

    @classmethod
    def fail(cls, status: Status, message: str, step: str | None = None,
             candidates: list[str] | tuple[str, ...] = ()) -> "Result":
        return cls(status=status, message=message, failed_step=step, candidates=list(candidates))
