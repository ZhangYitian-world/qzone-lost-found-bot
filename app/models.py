from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        raise ValueError("时间必须包含时区，例如 2026-10-24T20:00:00+08:00")
    return value


class PostInput(StrictModel):
    external_id: str | None = Field(default=None, max_length=200)
    published_at: datetime | None
    raw_time: str | None = None
    text: str = Field(max_length=50000)
    publisher: str | None = None
    original_url: str | None = None
    images: list[str] = Field(default_factory=list, max_length=12)
    forward_text: str | None = None
    original_ref: str | None = None
    content_complete: bool = True
    video_unreadable: bool = False

    _aware = field_validator("published_at")(aware)


class ImportInput(StrictModel):
    source_id: str
    posts: list[PostInput] = Field(min_length=1, max_length=500)


class SourceUpdate(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    qzone_id: str = Field(pattern=r"^[1-9][0-9]{4,14}$")
    enabled: bool = False
    initial_since: datetime
    pagination_verified: bool = False
    validation_notes: str = Field(default="", max_length=4000)
    _aware = field_validator("initial_since")(aware)


class Evidence(StrictModel):
    kind: Literal["text", "image", "manual"]
    quote: str = Field(min_length=1, max_length=2000)
    image_index: int | None = Field(default=None, ge=0, le=11)
    location: str | None = Field(default=None, max_length=300)


class Person(StrictModel):
    role: Literal["owner", "card_holder", "publisher", "proxy", "space_owner", "unknown"]
    name: str | None = Field(default=None, max_length=100)
    identifier: str | None = Field(default=None, max_length=100)
    school: str | None = None
    campus: str | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)

    @field_validator("name", "identifier")
    @classmethod
    def nonblank_identity(cls, value):
        return value.strip() or None if value is not None else None


class ExtractedEvent(StrictModel):
    event_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    event_type: Literal["lost_item", "found_item", "resolved", "other", "uncertain"]
    items: list[str] = Field(default_factory=list, max_length=30)
    event_time: str | None = None
    locations: dict[Literal["lost", "found", "stored"], str | None] = Field(default_factory=dict)
    claim_instructions: str | None = None
    persons: list[Person] = Field(default_factory=list, max_length=30)
    evidence: list[Evidence] = Field(default_factory=list, max_length=40)
    missing_fields: list[str] = Field(default_factory=list, max_length=30)
    conflicts: list[str] = Field(default_factory=list, max_length=30)
    reason: str = Field(min_length=1, max_length=3000)


class Extraction(StrictModel):
    events: list[ExtractedEvent] = Field(min_length=1, max_length=20)


class Correction(StrictModel):
    expected_version: int = Field(ge=1)
    operator: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=2000)
    event: ExtractedEvent


class Operation(StrictModel):
    expected_version: int = Field(ge=1)
    operator: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)


class Merge(Operation):
    target_id: str
    target_version: int = Field(ge=1)
