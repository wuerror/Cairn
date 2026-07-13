from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


FactType = Literal["source", "sink", "dataflow", "constraint", "gadget", "reachability", "verification"]
ConfidenceLevel = Literal["hypothesized", "static-confirmed", "reachable-confirmed", "poc-confirmed", "refuted"]


class Settings(BaseModel):
    intent_timeout: int = Field(ge=5)
    reason_timeout: int = Field(ge=5)


class Fact(BaseModel):
    id: str
    description: str
    type: FactType | None = None
    confidence: ConfidenceLevel | None = None
    locations: list[str] | None = None
    code_version: str | None = None
    evidence: str | None = None
    verifies: str | None = None
    intent_id: str | None = None
    batch_id: str | None = None
    effective_confidence: ConfidenceLevel | None = None
    stale: bool = False


class Observation(BaseModel):
    type: FactType | None = None
    description: str
    locations: list[str] | None = None
    evidence: str | None = None
    oracle_draft: str | None = None


class Intent(BaseModel):
    id: str
    from_: list[str] = Field(alias="from")
    to: str | None = None
    description: str
    creator: str
    worker: str | None = None
    last_heartbeat_at: str | None = None
    created_at: str
    concluded_at: str | None = None

    model_config = {"populate_by_name": True}


class Hint(BaseModel):
    id: str
    content: str
    creator: str
    created_at: str


class ProjectReason(BaseModel):
    worker: str
    trigger: str
    started_at: str
    last_heartbeat_at: str


class ProjectMeta(BaseModel):
    id: str
    title: str
    status: Literal["active", "stopped", "completed"]
    bootstrap_enabled: bool
    created_at: str
    reason: ProjectReason | None = None


class ProjectSummary(ProjectMeta):
    fact_count: int
    intent_count: int
    working_intent_count: int
    unclaimed_intent_count: int
    hint_count: int


class ProjectDetail(BaseModel):
    project: ProjectMeta
    facts: list[Fact]
    intents: list[Intent]
    hints: list[Hint]


class CreateHintInline(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateProjectRequest(BaseModel):
    title: str
    origin: str
    goal: str
    bootstrap_enabled: bool = True
    hints: list[CreateHintInline] | None = None

    @field_validator("title", "goal")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text
        if not isinstance(parsed, dict):
            return text
        if "codebase" not in parsed or not isinstance(parsed.get("codebase"), dict):
            raise ValueError("origin JSON must contain 'codebase' object with 'path'")
        cb = parsed["codebase"]
        if not isinstance(cb.get("path"), str) or not cb["path"].strip():
            raise ValueError("origin codebase.path is required")
        if "target" in parsed:
            target = parsed["target"]
            if not isinstance(target, dict):
                raise ValueError("origin target must be an object")
            if not isinstance(target.get("base_url"), str) or not target["base_url"].strip():
                raise ValueError("origin target.base_url is required when target is present")
        if "allowlist" in parsed and not isinstance(parsed["allowlist"], list):
            raise ValueError("origin allowlist must be an array")
        return text


class CreateHintRequest(BaseModel):
    content: str
    creator: str

    @field_validator("content", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class CreateIntentRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    creator: str
    worker: str | None = None

    model_config = {"populate_by_name": True}

    @field_validator("description", "creator", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class HeartbeatRequest(BaseModel):
    worker: str

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReasonClaimRequest(BaseModel):
    worker: str
    trigger: str

    @field_validator("worker", "trigger")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ConcludeRequest(BaseModel):
    worker: str
    description: str | None = None
    observations: list[Observation] | None = None

    @field_validator("worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @model_validator(mode="after")
    def validate_payload(self) -> "ConcludeRequest":
        if self.description is not None and self.observations is not None:
            raise ValueError("description and observations cannot coexist")
        if self.description is not None:
            text = self.description.strip()
            if not text:
                raise ValueError("description must not be empty")
        if self.observations is not None and len(self.observations) == 0:
            raise ValueError("observations must not be empty")
        if self.description is None and self.observations is None:
            raise ValueError("either description or observations is required")
        return self


class CompleteRequest(BaseModel):
    from_: list[str] = Field(alias="from", min_length=1)
    description: str
    worker: str

    model_config = {"populate_by_name": True}

    @field_validator("description", "worker")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text

    @field_validator("from_")
    @classmethod
    def validate_fact_ids(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = item.strip()
            if not text:
                raise ValueError("fact ids must not be empty")
            cleaned.append(text)
        return cleaned


class ConcludeResponse(BaseModel):
    fact: Fact | None = None
    facts: list[Fact] = Field(default_factory=list)
    intent: Intent


class UpdateProjectStatusRequest(BaseModel):
    status: Literal["active", "stopped"]


class UpdateProjectTitleRequest(BaseModel):
    title: str

    @field_validator("title")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenRequest(BaseModel):
    description: str
    creator: str

    @field_validator("description", "creator")
    @classmethod
    def validate_non_empty_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be empty")
        return text


class ReopenResponse(BaseModel):
    project: ProjectMeta
    fact: Fact
    intent: Intent
