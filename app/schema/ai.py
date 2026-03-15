"""
AI-related services and utilities.
"""


from google.genai import types
from pydantic import BaseModel, Field


class ChatResponse(BaseModel):
    text: str = Field(description="The text of the chat response.")
    usage_metadata: types.GenerateContentResponseUsageMetadata | None = Field(
        None,
        description="Metadata about the usage of the response, such as token counts.",
    )
