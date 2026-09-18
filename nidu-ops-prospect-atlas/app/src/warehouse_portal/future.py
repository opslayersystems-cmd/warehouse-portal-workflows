"""Future outreach and reply contracts; no email workflow runs here."""

from typing import Protocol

from pydantic import BaseModel, Field


class OutreachDraftOutput(BaseModel):
    account_id: str
    subject: str
    body: str
    evidence_refs: list[str]


class ComplianceOutput(BaseModel):
    approved_for_draft: bool
    findings: list[str] = Field(default_factory=list)


class ReplyTriageOutput(BaseModel):
    category: str
    confidence: float
    requires_human_review: bool = True


class NextActionOutput(BaseModel):
    action: str
    reason: str
    requires_human_review: bool = True


class OutreachWriterAgent(Protocol):
    def run(self, account_id: str) -> OutreachDraftOutput: ...


class ComplianceAgent(Protocol):
    def run(self, draft: OutreachDraftOutput) -> ComplianceOutput: ...


class ReplyTriageAgent(Protocol):
    def run(self, reply_id: str) -> ReplyTriageOutput: ...


class NextActionPlannerAgent(Protocol):
    def run(self, account_id: str) -> NextActionOutput: ...
