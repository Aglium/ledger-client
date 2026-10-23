"""The Layer 0 project card: what a repo is, and what it exchanges.

Field-level, not endpoint-level, on purpose. "`/orders` changed" is not
actionable; "`orders[].customerId` was removed and the billing service reads
it" is. The removal that breaks a downstream service is a field event, so
fields are the unit.

Consumed contracts matter as much as published ones and are harder to get:
nobody declares "I depend on this key being present". That dependency lives in
code, which is why it has to be derived rather than read off a manifest.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, field_validator


class ContractKind(str, Enum):
    HTTP_API = "http_api"
    EVENT = "event"
    QUEUE_MESSAGE = "queue_message"
    DB_TABLE = "db_table"
    UI_CONTRACT = "ui_contract"


class Direction(str, Enum):
    PUBLISHES = "publishes"
    READS = "reads"
    WRITES = "writes"
    REQUIRES_PRESENT = "requires_present"


class ContractField(BaseModel):
    name: str = Field(description="Field name exactly as it appears in the source")
    path: str | None = Field(None, description="JSON path if nested, e.g. orders[].customerId")
    type_hint: str | None = None
    role: str | None = Field(None, description="What this field MEANS, one clause")
    optional: bool | None = None

    @property
    def qualified(self) -> str:
        return self.path or self.name


class Contract(BaseModel):
    kind: ContractKind
    name: str = Field(description="Endpoint path, event name, queue/topic, or table")
    carrier: str | None = Field(None, description="Transport identifier: topic, exchange, route")
    direction: Direction
    fields: list[ContractField] = Field(default_factory=list)
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("contract name cannot be empty")
        return v.strip()


class Entity(BaseModel):
    name: str
    fields: list[ContractField] = Field(default_factory=list)


class ProjectCard(BaseModel):
    repo: str
    purpose: str = Field(description="What this service is for, one or two sentences")
    domains: list[str] = Field(default_factory=list)
    stage: str | None = Field(None, description="e.g. active, legacy, template, reference")
    tech: list[str] = Field(default_factory=list)
    contracts: list[Contract] = Field(default_factory=list)
    entities: list[Entity] = Field(default_factory=list)
    notes: str | None = None

    def claimed_names(self) -> list[str]:
        """Every name the model asserted, for grounding against the evidence.

        Purpose and domains are prose and deliberately excluded -- grounding
        applies to identifiers, which either exist in the source or do not.
        """
        out: list[str] = []
        for c in self.contracts:
            out.append(c.name)
            if c.carrier:
                out.append(c.carrier)
            out.extend(f.qualified for f in c.fields)
        for e in self.entities:
            out.append(e.name)
            out.extend(f.qualified for f in e.fields)
        return [n for n in out if n]

    def drop_names(self, names: set[str]) -> "ProjectCard":
        """Return a copy without the given names. Used to strip ungrounded claims."""
        kept_contracts = []
        for c in self.contracts:
            if c.name in names:
                continue
            c = c.model_copy(update={"fields": [f for f in c.fields if f.qualified not in names]})
            kept_contracts.append(c)
        kept_entities = []
        for e in self.entities:
            if e.name in names:
                continue
            kept_entities.append(e.model_copy(update={"fields": [f for f in e.fields if f.qualified not in names]}))
        return self.model_copy(update={"contracts": kept_contracts, "entities": kept_entities})


CARD_JSON_SCHEMA = ProjectCard.model_json_schema()
