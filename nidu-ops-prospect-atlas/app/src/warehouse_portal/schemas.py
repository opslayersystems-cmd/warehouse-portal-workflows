import re
from datetime import datetime
from enum import StrEnum
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


class EvidenceLevel(StrEnum):
    VERIFIED_FACT = "VERIFIED_FACT"
    STRONG_INFERENCE = "STRONG_INFERENCE"
    WEAK_INFERENCE = "WEAK_INFERENCE"
    UNKNOWN = "UNKNOWN"


class ProductStatus(StrEnum):
    VERIFIED_EXISTING = "VERIFIED_EXISTING"
    DEV_ONLY = "DEV_ONLY"
    PLANNED = "PLANNED"
    CLIENT_SPECIFIC = "CLIENT_SPECIFIC"
    UNKNOWN = "UNKNOWN"


class Candidate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    website: str | None = None
    industry: str | None = None
    description: str | None = None
    headquarters: str | None = None
    local_address: str | None = None
    city: str | None = None
    state: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    source_url: str | None = None
    source_title: str | None = None
    source_type: str | None = None
    is_demo: bool = False

    @field_validator("website", "source_url")
    @classmethod
    def safe_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Only HTTP(S) URLs are accepted")
        return value


class EvidenceInput(BaseModel):
    source_url: HttpUrl
    source_title: str = Field(min_length=3)
    retrieved_at: datetime
    source_type: str
    supported_claim: str = Field(min_length=5)
    summary: str = Field(min_length=5)
    evidence_level: EvidenceLevel
    freshness_status: str = "CURRENT"
    claim_type: str = "general"

    @model_validator(mode="after")
    def no_unsupported_fact(self) -> "EvidenceInput":
        if self.evidence_level == EvidenceLevel.VERIFIED_FACT and self.source_type in {
            "manual_unsourced",
            "llm_unsourced",
            "unknown",
        }:
            raise ValueError("Verified facts require an attributable source")
        return self


class ResearchOutput(BaseModel):
    account_summary: str
    evidence: list[EvidenceInput] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)


class DiscoveryOutput(BaseModel):
    candidates: list[Candidate]


class AuditOutput(BaseModel):
    accepted: list[EvidenceInput]
    rejected_count: int


class ScoreOutput(BaseModel):
    score: int
    grade: str
    confidence_score: int
    component_scores: dict[str, int]
    penalties: dict[str, int]
    evidence_refs: list[str]
    rationale: str
    uncertainties: list[str]
    disqualifying_questions: list[str]
    sales_cycle_category: str
    implementation_complexity: str
    support_burden: str
    recommended_next_action: str


class ClassificationOutput(BaseModel):
    classification: str
    rationale: str


class OrchestrationOutput(BaseModel):
    account_id: str
    stage: str
    source_count: int
    score: int | None = None


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = None


class LoginInput(BaseModel):
    username: str
    password: str


class DiscoveryRequest(BaseModel):
    provider: str = "mock"
    city: str = "Savannah"
    industry: str = "industrial supply distributor"


class ContactCandidate(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    title: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    source_url: HttpUrl
    source_title: str = Field(min_length=3, max_length=500)
    source_type: str = Field(min_length=3, max_length=60)
    retrieved_at: datetime

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        normalized = value.strip().casefold()
        if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", normalized):
            raise ValueError("Contact email must be a valid address")
        return normalized


class ContactResearchOutput(BaseModel):
    contacts: list[ContactCandidate] = Field(default_factory=list)


class SuppressionInput(BaseModel):
    kind: Literal["email", "domain", "account"]
    value: str = Field(min_length=3, max_length=255)
    reason: str = Field(min_length=3, max_length=1000)
