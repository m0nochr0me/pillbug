"""
Log entry schema definition.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class LogEntry(BaseModel):
    asctime: datetime = Field(..., description="Timestamp of the log entry")
    levelname: str = Field(..., description="Log level name")
    message: str = Field(..., description="Log message content")

    @field_serializer("asctime")
    def serialize_asctime(self, asctime: datetime) -> str:
        return asctime.strftime(r"%Y-%m-%d %H:%M:%S,%f")[:-3]

    model_config = ConfigDict(from_attributes=True)
