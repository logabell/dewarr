from types import SimpleNamespace

from app.importing.release_narrators import naming_narrators


def test_narrator_fallback_uses_unambiguous_mam_credits_and_preserves_stronger_sources():
    group = SimpleNamespace(medium="audio", narrators=[])
    version = SimpleNamespace(narrators=[])
    release = {"source": "mam", "medium": "audio", "narrators": ["Casey Reed"]}
    assert naming_narrators(group, version, [release]) == ["Casey Reed"]
    assert naming_narrators(group, version, [{**release, "source": "prowlarr"}]) == []
    assert naming_narrators(group, version, [release, {**release, "narrators": ["Other"]}]) == []
    version.narrators = ["Catalog reader"]
    assert naming_narrators(group, version, [release]) == ["Catalog reader"]
    group.narrators = ["File reader"]
    assert naming_narrators(group, version, [release]) == ["File reader"]
