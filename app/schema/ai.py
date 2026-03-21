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


class ChatSessionUsageTotals(BaseModel):
    prompt_token_count: int = Field(default=0, description="Accumulated prompt tokens across the session.")
    candidates_token_count: int = Field(default=0, description="Accumulated model output tokens across the session.")
    total_token_count: int = Field(
        default=0, description="Accumulated total tokens reported by Gemini across the session."
    )
    thoughts_token_count: int = Field(default=0, description="Accumulated internal thinking tokens reported by Gemini.")
    tool_use_prompt_token_count: int = Field(
        default=0, description="Accumulated tool-use prompt tokens across the session."
    )
    cached_content_token_count: int = Field(
        default=0, description="Accumulated cached-content tokens across the session."
    )

    def add_usage_metadata(self, usage_metadata: types.GenerateContentResponseUsageMetadata | None) -> None:
        if usage_metadata is None:
            return

        self.prompt_token_count += usage_metadata.prompt_token_count or 0
        self.candidates_token_count += usage_metadata.candidates_token_count or 0
        self.total_token_count += usage_metadata.total_token_count or 0
        self.thoughts_token_count += usage_metadata.thoughts_token_count or 0
        self.tool_use_prompt_token_count += usage_metadata.tool_use_prompt_token_count or 0
        self.cached_content_token_count += usage_metadata.cached_content_token_count or 0

    def to_display_text(self) -> str:
        lines = [
            "Session token usage:",
            f"Input tokens: {self.prompt_token_count}",
            f"Output tokens: {self.candidates_token_count}",
            f"Total tokens: {self.total_token_count}",
        ]

        if self.tool_use_prompt_token_count:
            lines.append(f"Tool prompt tokens: {self.tool_use_prompt_token_count}")
        if self.thoughts_token_count:
            lines.append(f"Thinking tokens: {self.thoughts_token_count}")
        if self.cached_content_token_count:
            lines.append(f"Cached content tokens: {self.cached_content_token_count}")

        return "\n".join(lines)


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
    usage_totals: ChatSessionUsageTotals = Field(
        default_factory=ChatSessionUsageTotals,
        description="Accumulated token usage totals for the session.",
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
