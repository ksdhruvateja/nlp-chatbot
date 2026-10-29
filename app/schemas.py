"""The common diagnosis format every AI adapter is normalized into, plus API shapes."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

ServiceCategory = Literal["plumbing", "electrical", "hvac", "appliance", "roofing", "structural",
                          "gas", "general"]
SERVICE_CATEGORIES: tuple[str, ...] = ServiceCategory.__args__  # type: ignore[attr-defined]


class Cause(BaseModel):
    cause: str = Field(min_length=3)
    likelihood: float = Field(ge=0, le=1)
    reasoning: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class Step(BaseModel):
    title: str = Field(min_length=3)
    detail: str = ""
    tools: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    hazard_tags: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class PartNeeded(BaseModel):
    part_type: str          # normalized snake_case key, e.g. "anode_rod"
    description: str = ""
    quantity: int = Field(default=1, ge=1, le=100)


class Diagnosis(BaseModel):
    """Common diagnosis format (output of 'Normalize Response')."""
    summary: str = Field(min_length=10)
    likely_causes: list[Cause] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    recommended_action: Literal["diy", "professional", "clarify"]
    hazards: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    uncertainty_notes: list[str] = Field(default_factory=list)
    proposed_steps: list[Step] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    parts_needed: list[PartNeeded] = Field(default_factory=list)
    estimated_time_minutes: int | None = Field(default=None, ge=0, le=60 * 24 * 7)
    skill_level: Literal["beginner", "intermediate", "advanced"] = "intermediate"

    @field_validator("hazards")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return [h.strip().lower() for h in v if h.strip()]


class ClarificationQuestion(BaseModel):
    id: str
    question: str
    kind: Literal["text", "photo", "choice"] = "text"
    choices: list[str] = Field(default_factory=list)
    reason: str = ""


class SafetyWarning(BaseModel):
    code: str
    title: str
    message: str
    risks: list[str]
    severity: Literal["advisory", "warning", "high", "critical"]


def diagnosis_json_schema() -> dict:
    return Diagnosis.model_json_schema()
