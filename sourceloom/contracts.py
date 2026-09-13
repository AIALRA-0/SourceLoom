"""Versioned artifacts; role responses cannot supply workflow authority."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(Strict):
    source_id: str
    quote: str = Field(min_length=1)


class Obligation(Strict):
    id: str
    object_id: str
    statement: str
    conditions: list[str] = []
    quantities: list[str] = []
    negations: list[str] = []
    status: Literal["unreviewed", "reviewed"] = "unreviewed"


class InventoryReview(Strict):
    findings: list[str]
    additions: list[Obligation]
    assessed_object_ids: list[str]
    unresolved_object_ids: list[str]


class Unit(Strict):
    id: str
    title: str
    objective: str
    obligation_ids: list[str] = Field(min_length=1)
    prerequisites: list[str]
    stages: list[str] = Field(min_length=7, max_length=7)
    object_ids: list[str]
    proof_questions: list[str]


class Plan(Strict):
    title: str
    objective: str
    units: list[Unit] = Field(min_length=1)
    research_gaps: list[str]


class Block(Strict):
    id: str
    unit_id: str
    kind: Literal["source", "explanation", "example", "exercise", "extension", "object"]
    markdown: str
    obligation_ids: list[str]
    object_ids: list[str]
    evidence: list[Evidence]
    embedded_object_ids: list[str] = []


class Draft(Strict):
    blocks: list[Block] = Field(min_length=1)


class Finding(Strict):
    id: str
    severity: Literal["error", "warning"]
    block_id: str
    obligation_id: str
    message: str
    expected: str


class Proof(Strict):
    id: str
    block_id: str
    claim: str
    kind: Literal["mathematical", "empirical", "source", "causal", "design", "example"]
    assumptions: list[str]
    evidence: list[Evidence]
    method: str
    status: Literal["supported", "unresolved", "refuted"]
    limitation: str


class Review(Strict):
    findings: list[Finding]
    assessed_obligation_ids: list[str]
    assessed_block_ids: list[str]
    proofs: list[Proof]
    teaching_notes: list[str]


class Edit(Strict):
    block_id: str
    old_markdown: str = Field(min_length=1)
    new_markdown: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class Patch(Strict):
    base_revision: int = Field(ge=1)
    edits: list[Edit] = Field(min_length=1)


class ResearchPlan(Strict):
    objective: str
    questions: list[str] = Field(min_length=1, max_length=12)
    queries: list[str] = Field(min_length=1, max_length=12)
    stopping_conditions: list[str] = Field(min_length=1)
