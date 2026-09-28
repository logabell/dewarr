"""Frozen acquisition limits; these never redefine whether a book is already owned."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.release_profiles import FORMATS, ReleasePreferences

PRIMARY_FORMATS = {
    "ebook": set(ReleasePreferences().ebook_formats),
    "audio": set(ReleasePreferences().audio_formats),
}


class DownloadConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocked_formats: list[str] = Field(default_factory=list, max_length=20)
    maximum_bytes: int | None = Field(
        default=None,
        gt=0,
        le=2**53 - 1,
        json_schema_extra={"deprecated": True},
        description="Legacy compatibility value; transfer sizes are not capped",
    )

    @field_validator("blocked_formats")
    @classmethod
    def formats(cls, values):
        normalized = {value.strip().lower().lstrip(".") for value in values}
        if not normalized <= FORMATS:
            raise ValueError("Use supported format names")
        return sorted(normalized)

    @property
    def active(self):
        return bool(self.blocked_formats or self.maximum_bytes is not None)


def combine(left, right):
    a = DownloadConstraints.model_validate(left or {})
    b = DownloadConstraints.model_validate(right or {})
    limits = [limit for limit in (a.maximum_bytes, b.maximum_bytes) if limit is not None]
    merged = DownloadConstraints(
        blocked_formats=sorted(set(a.blocked_formats) | set(b.blocked_formats)),
        maximum_bytes=min(limits) if limits else None,
    )
    return merged.model_dump() if merged.active else None


def constrained_preferences(preferences, rule):
    constraints = combine(
        preferences.model_dump(include={"blocked_formats", "maximum_bytes"}),
        rule.get("download_constraints"),
    )
    return preferences.model_copy(
        update={
            **(constraints or {}),
            "required_narrators": rule.get("required_narrators", []),
        }
    )


def formats_possible(constraints, medium):
    return bool(PRIMARY_FORMATS[medium] - set((constraints or {}).get("blocked_formats", [])))
