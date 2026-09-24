import json
import subprocess
from copy import deepcopy
from unittest.mock import Mock

import pytest

from scripts import release

SHA = "a" * 40
OTHER_SHA = "b" * 40
DIGEST = "sha256:" + "c" * 64
OTHER_DIGEST = "sha256:" + "d" * 64
REPO = "logabell/dewarr"
EXPECTED = {
    "schema": 1,
    "repository": REPO,
    "sha": SHA,
    "version": "v0.2.3",
    "image": "ghcr.io/" + REPO,
}
RECORD = {**EXPECTED, "digest": DIGEST}
MANIFEST = {
    "digest": DIGEST,
    "manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}},
        {"platform": {"os": "linux", "architecture": "arm64"}},
        {
            "annotations": {"vnd.docker.reference.type": "attestation-manifest"},
            "platform": {"os": "unknown", "architecture": "unknown"},
        },
    ],
}
RUN = {
    "id": 42,
    "head_sha": SHA,
    "head_branch": "main",
    "event": "push",
    "path": release.WORKFLOW,
    "head_repository": {"full_name": REPO},
    "status": "completed",
    "conclusion": "success",
}
ARTIFACT = {"name": release.ARTIFACT, "expired": False, "size_in_bytes": 1024}


def request_for(runs, artifacts=None):
    return Mock(
        side_effect=lambda path: (
            {"artifacts": artifacts if artifacts is not None else [ARTIFACT]}
            if "/artifacts?" in path
            else {"workflow_runs": runs}
        )
    )


def test_candidate_reuses_only_a_successful_main_push():
    request = request_for([RUN])
    assert release.candidate_run(REPO, SHA, request) == "42"
    assert "head_sha=" + SHA in request.call_args_list[0].args[0]


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", OTHER_SHA),
        ("head_branch", "dev"),
        ("event", "pull_request"),
        ("event", "workflow_dispatch"),
        ("path", ".github/workflows/other.yml"),
        ("head_repository", {"full_name": "someone/fork"}),
    ],
)
def test_other_sources_never_authorize_promotion(field, value):
    request = request_for([{**RUN, field: value}])
    assert release.candidate_run(REPO, SHA, request) == ""
    assert request.call_count == 1


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped"])
def test_failed_latest_run_cannot_fall_back_to_an_older_success(conclusion):
    request = request_for([RUN, {**RUN, "id": 43, "conclusion": conclusion}])
    with pytest.raises(ValueError, match="latest main candidate failed"):
        release.candidate_run(REPO, SHA, request)


@pytest.mark.parametrize("artifacts", [[], [{**ARTIFACT, "expired": True}]])
def test_missing_or_expired_evidence_requires_full_validation(artifacts):
    assert release.candidate_run(REPO, SHA, request_for([RUN], artifacts)) == ""


@pytest.mark.parametrize(
    "artifacts", [[ARTIFACT, ARTIFACT], [{**ARTIFACT, "size_in_bytes": 50000}]]
)
def test_unexpected_artifact_is_not_treated_as_success(artifacts):
    with pytest.raises(ValueError, match="Unexpected candidate artifact"):
        release.candidate_run(REPO, SHA, request_for([RUN], artifacts))


def test_waits_for_active_main_candidate_without_duplicate_build():
    request = Mock(
        side_effect=[
            {"workflow_runs": [{**RUN, "status": "in_progress", "conclusion": None}]},
            {"workflow_runs": [RUN]},
            {"artifacts": [ARTIFACT]},
        ]
    )
    sleep = Mock()
    assert release.candidate_run(REPO, SHA, request, clock=lambda: 0, sleep=sleep) == "42"
    sleep.assert_called_once_with(15)


def test_wait_is_bounded_and_api_errors_never_allow_promotion():
    with pytest.raises(ValueError, match="still running"):
        release.candidate_run(REPO, SHA, request_for([{**RUN, "status": "queued"}]), wait_seconds=0)
    with pytest.raises(subprocess.CalledProcessError):
        release.candidate_run(REPO, SHA, Mock(side_effect=subprocess.CalledProcessError(1, "gh")))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", 2),
        ("sha", OTHER_SHA),
        ("repository", "someone/fork"),
        ("image", "ghcr.io/someone/other"),
        ("version", "v9.9.9"),
        ("digest", "latest"),
        ("digest", DIGEST + "\ntag=latest"),
        ("digest", None),
    ],
)
def test_candidate_metadata_is_bound_to_exact_source_and_version(field, value):
    with pytest.raises(ValueError):
        release.validate({**RECORD, field: value}, EXPECTED)


def test_valid_candidate_preserves_immutable_digest():
    assert release.validate(RECORD, EXPECTED)["digest"] == DIGEST
    assert release.verify_manifest(MANIFEST, DIGEST) == DIGEST


@pytest.mark.parametrize(
    "architectures", [["amd64"], ["arm64"], ["amd64", "amd64"], ["amd64", "arm64", "386"]]
)
def test_manifest_requires_both_tested_architectures_exactly_once(architectures):
    manifest = {
        "digest": DIGEST,
        "manifests": [{"platform": {"os": "linux", "architecture": a}} for a in architectures],
    }
    with pytest.raises(ValueError, match="exactly the tested"):
        release.verify_manifest(manifest)


def test_digest_mismatch_rejects_registry_manifest():
    with pytest.raises(ValueError, match="does not match"):
        release.verify_manifest(MANIFEST, OTHER_DIGEST)


def test_main_only_updates_edge_and_superseded_main_updates_no_channels():
    assert release.channel_tags("refs/heads/main", "v0.2.3", SHA, SHA, []) == ["edge"]
    assert release.channel_tags("refs/heads/main", "v0.2.3", SHA, OTHER_SHA, []) == []


def test_old_release_cannot_downgrade_latest_and_semver_sort_is_numeric():
    tags = ["v0.2.3", "v0.2.10", "v0.3.0-rc.1", "not-a-release"]
    assert release.channel_tags("refs/tags/v0.2.3", "v0.2.3", SHA, SHA, tags) == ["v0.2.3"]
    assert release.channel_tags("refs/tags/v0.2.10", "v0.2.10", SHA, SHA, tags) == [
        "v0.2.10",
        "latest",
    ]


@pytest.mark.parametrize("ref", ["refs/heads/dev", "refs/tags/v0.2.2", "refs/tags/v0.2.3;bad"])
def test_wrong_ref_or_tag_version_cannot_publish(ref):
    with pytest.raises(ValueError, match="exactly match"):
        release.channel_tags(ref, "v0.2.3", SHA, SHA, [])


def test_existing_version_cannot_be_overwritten_by_rebuilt_candidate():
    read = Mock(side_effect=[MANIFEST, {**MANIFEST, "digest": OTHER_DIGEST}])
    run = Mock()
    with pytest.raises(ValueError, match="immutable"):
        release.promote(RECORD, ["v0.2.3", "latest"], run, read)
    run.assert_not_called()


@pytest.mark.parametrize("existing", [None, MANIFEST])
def test_promotion_and_retry_copy_only_the_verified_digest(existing):
    read = Mock(side_effect=[MANIFEST, existing, MANIFEST, MANIFEST])
    run = Mock()
    release.promote(RECORD, ["v0.2.3", "latest"], run, read)
    assert [call.args[-1] for call in run.call_args_list] == [RECORD["image"] + "@" + DIGEST] * 2
    assert [call.args[-2] for call in run.call_args_list] == [
        RECORD["image"] + ":v0.2.3",
        RECORD["image"] + ":latest",
    ]


def test_partial_publication_failure_stops_before_latest():
    run = Mock(side_effect=RuntimeError("registry unavailable"))
    with pytest.raises(RuntimeError):
        release.promote(RECORD, ["v0.2.3", "latest"], run, Mock(side_effect=[MANIFEST, None]))
    assert run.call_count == 1


@pytest.mark.parametrize(
    "error", ["unauthorized", "connection timeout", "rate limited", "not found"]
)
def test_registry_errors_cannot_be_confused_with_missing_version(monkeypatch, error):
    monkeypatch.setattr(
        subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 1, "", error))
    )
    with pytest.raises(RuntimeError):
        release.inspect("ghcr.io/logabell/dewarr:v0.2.3", missing_ok=True)


def test_explicit_missing_manifest_allows_new_version(monkeypatch):
    ref = "ghcr.io/logabell/dewarr:v0.2.3"
    monkeypatch.setattr(
        subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 1, "", f"ERROR: {ref}: not found\n")),
    )
    assert release.inspect(ref, missing_ok=True) is None


def test_version_requires_matching_backend_frontend_and_lockfile(tmp_path):
    (tmp_path / "apps/web").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.2.3"\n')
    (tmp_path / "apps/web/package.json").write_text(json.dumps({"version": "0.2.3"}))
    lock = {"version": "0.2.3", "packages": {"": {"version": "0.2.3"}}}
    path = tmp_path / "apps/web/package-lock.json"
    path.write_text(json.dumps(lock))
    assert release.version(tmp_path) == "v0.2.3"
    stale = deepcopy(lock)
    stale["packages"][""]["version"] = "0.2.2"
    path.write_text(json.dumps(stale))
    with pytest.raises(ValueError, match="same stable release version"):
        release.version(tmp_path)
