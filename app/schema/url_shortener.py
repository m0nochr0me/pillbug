from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ShortUrlRecord(BaseModel):
    token: str = Field(
        ...,
        description="Stable short token for the original URL",
        min_length=1,
    )
    url: str = Field(
        ...,
        description="Original long URL",
        min_length=1,
    )
    created_at: datetime = Field(
        default_factory=_utcnow,
        description="UTC timestamp when the short URL token was created",
    )

    model_config = ConfigDict(extra="ignore")


class ShortUrlStore(BaseModel):
    urls: list[ShortUrlRecord] = Field(
        default_factory=list,
        description="Stored local short URL mappings",
    )

    model_config = ConfigDict(extra="ignore")
