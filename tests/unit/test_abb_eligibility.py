from datetime import UTC, datetime
from types import SimpleNamespace

from app.adapters.audiobookbay import ABBRelease
from app.domain.automatic_eligibility import eligibility
from app.domain.release_profiles import PreferenceOverrides, ReleasePreferences, resolve_preferences
from tests.abb_fixture import PATH


def test_unknown_seed_policy_preserves_legacy_snapshots_and_explicit_disable():
    assert "allow_unknown_seeders" not in ReleasePreferences().model_dump()
    assert PreferenceOverrides(allow_unknown_seeders=False).model_dump() == {
        "allow_unknown_seeders": False
    }
    values, origins = resolve_preferences(
        [
            ("Profile", {"allow_unknown_seeders": True}),
            ("Request", {"allow_unknown_seeders": False}),
        ]
    )
    assert not values.allow_unknown_seeders and origins["allow_unknown_seeders"] == "Request"


def test_incomplete_metadata_preference_preserves_legacy_snapshots_and_explicit_disable():
    assert "allow_incomplete_release_metadata" not in ReleasePreferences().model_dump()
    assert PreferenceOverrides(allow_incomplete_release_metadata=False).model_dump() == {
        "allow_incomplete_release_metadata": False
    }
    values, origins = resolve_preferences(
        [
            ("Profile", {"allow_incomplete_release_metadata": True}),
            ("Request", {"allow_incomplete_release_metadata": False}),
        ]
    )
    assert not values.allow_incomplete_release_metadata
    assert origins["allow_incomplete_release_metadata"] == "Request"


def test_unknown_seed_override_never_invents_availability_or_allows_reported_zero():
    release = ABBRelease(
        source_id="fixture",
        title="Harbor",
        raw_title="Harbor",
        authors=["Writer"],
        formats=["m4b"],
        language="en",
        detail_path=PATH,
        observed_at=datetime.now(UTC),
    )
    work = {"title": "Harbor", "authors": ["Writer"]}
    rule = {"medium": "audio", "language": "en", "abridged": None, "version_id": None}
    defaults = ReleasePreferences()
    assert any("seeder" in reason for reason in eligibility(release, work, rule, defaults))
    enabled = ReleasePreferences(allow_unknown_seeders=True)
    assert not eligibility(release, work, rule, enabled)
    assert release.seeders is None
    zero = release.model_copy(update={"seeders": 0})
    assert any("seeder" in reason for reason in eligibility(zero, work, rule, enabled))
    # Unknown seeds remain blocked for other sources, even when opting into ABB metadata.
    other = SimpleNamespace(**{**release.model_dump(), "source": "prowlarr"})
    assert any("seeder" in reason for reason in eligibility(other, work, rule, enabled))
