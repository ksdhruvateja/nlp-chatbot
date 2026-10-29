"""Validated API shapes (Phase 8 'Return validated API response')."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import ServiceCategory

Outcome = Literal["diy_plan", "safety_warning", "clarification", "professional", "cancelled", "failure"]


class DiagnosisResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    version: int
    outcome: Outcome
    status: str
    summary: str
    likely_causes: list[dict] = Field(default_factory=list)
    confidence: float | None = None
    sources: list[dict] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    evidence_check: dict | None = None
    safety: dict | None = None
    repair_plan: dict | None = None
    clarification: dict | None = None
    professional_recommendation: dict | None = None
    limitations: list[str] = Field(default_factory=list)
    error: dict | None = None


class PropertyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    address: str = Field(default="", max_length=500)
    property_type: Literal["house", "apartment", "condo", "townhouse", "commercial", "other"] = "house"
    year_built: int | None = Field(default=None, ge=1600, le=2100)
    passport: dict[str, Any] = Field(default_factory=dict)


class EquipmentIn(BaseModel):
    service_category: ServiceCategory
    kind: str = Field(min_length=2, max_length=100)
    brand: str | None = Field(default=None, max_length=100)
    model_number: str | None = Field(default=None, max_length=60)
    serial_number: str | None = Field(default=None, max_length=60)
    install_year: int | None = Field(default=None, ge=1900, le=2100)
    notes: str = Field(default="", max_length=2000)


class SafetyChoiceIn(BaseModel):
    choice: Literal["continue", "cancel"]
    acknowledged_warning_codes: list[str] = Field(default_factory=list)


class OutcomeIn(BaseModel):
    resolution: Literal["fixed_diy", "fixed_professional", "not_fixed", "abandoned"]
    parts_used: list[dict] = Field(default_factory=list, max_length=50)
    actual_cost: float | None = Field(default=None, ge=0, le=1_000_000)
    rating: int | None = Field(default=None, ge=1, le=5)
    feedback: str = Field(default="", max_length=5000)


class OutcomeReviewIn(BaseModel):
    label: Literal["user_reported", "verified"]
    approved_for_eval: bool = False


class KnowledgeMetaIn(BaseModel):
    title: str
    source: str
    permission: Literal["approved", "licensed", "public_domain", "internal", "manufacturer_public"]
    service_category: ServiceCategory
    version: str
    publisher: str = ""
    brand: str | None = None
    model_numbers: list[str] = Field(default_factory=list)


class ProductIn(BaseModel):
    sku: str
    name: str
    brand: str | None = None
    service_category: ServiceCategory
    part_type: str
    compatible_models: list[str] = Field(default_factory=list)
    price: float = Field(ge=0)
    currency: str = "USD"
    price_source: str


class KnowledgeReviewIn(BaseModel):
    action: Literal["approve", "reject"]
    document_ids: list[str] = Field(default_factory=list, max_length=1000)
    brand: str | None = None
