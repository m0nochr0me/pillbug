"""
AI-related services and utilities.
"""

from pathlib import Path

from google.genai import types
from pydantic import BaseModel, ConfigDict, Field


class ChatResponse(BaseModel):
    text: str = Field(description="The text of the chat response.")
    usage_metadata: types.GenerateContentResponseUsageMetadata | None = Field(
        None,
        description="Metadata about the usage of the response, such as token counts.",
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
