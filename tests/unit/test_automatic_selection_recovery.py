from types import SimpleNamespace

from app.domain.automatic_selection import recovery_excludes_source


def test_recovery_excludes_only_the_exact_refused_soulseek_peer_folder():
    excluded = {"peer-folder-a", "peer-folder-b"}

    assert recovery_excludes_source(
        SimpleNamespace(source_key="slskd"),
        SimpleNamespace(source_id="peer-folder-a"),
        excluded,
    )
    assert not recovery_excludes_source(
        SimpleNamespace(source_key="slskd"),
        SimpleNamespace(source_id="peer-folder-c"),
        excluded,
    )
    assert not recovery_excludes_source(
        SimpleNamespace(source_key="mam"),
        SimpleNamespace(source_id="peer-folder-a"),
        excluded,
    )
