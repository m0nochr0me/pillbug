"""
AI-related services and utilities.
"""

from datetime import UTC, datetime
from pathlib import Path

from google.genai import types
from pydantic import BaseModel, ConfigDict, Field


class ChatResponse(BaseModel):
    text: str = Field(description="The text of the chat response.")
    usage_metadata: types.GenerateContentResponseUsageMetadata | None = Field(
        None,
        description="Metadata about the usage of the response, such as token counts.",
    )


class InboundAttachment(BaseModel):
    path: str = Field(description="Workspace-relative or workspace-contained absolute path to the attachment.")
    mime_type: str | None = Field(default=None, description="Declared MIME type for the attachment, if known.")
    display_name: str | None = Field(default=None, description="Preferred display name for the attachment upload.")
    source: str | None = Field(default=None, description="Attachment origin such as telegram, local, or fetched-url.")
    kind: str | None = Field(
        default=None, description="Channel-specific content kind such as photo, document, or audio."
    )

    model_config = ConfigDict(extra="ignore")


class ChatSessionSnapshot(BaseModel):
    session_id: str = Field(description="The stable session identifier used by the runtime.")
    updated_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        description="The last time this session history was written to disk.",
    )
    history: list[types.Content] = Field(
        default_factory=list,
        description="Curated Gemini chat history for restoring a prior session.",
    )


class Skill(BaseModel):
    name: str = Field(
        description="The name of the skill.",
    )
    description: str = Field(
        description="A brief description of the skill.",
    )
    location: Path = Field(
        description="The file path to the skill's implementation.",
    )

    model_config = ConfigDict(extra="ignore")
