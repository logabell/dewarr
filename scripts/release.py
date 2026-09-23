"""Build once, then promote a checked release candidate by immutable image digest.

Uses only the standard library and the runner's gh/Docker CLIs. Candidate artifacts
are accepted only from a successful main push of this workflow at the exact source SHA.
"""

import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from urllib.parse import urlencode

VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
WORKFLOW = ".github/workflows/container.yml"
ARTIFACT = "release-candidate"


def command(*args):
    return subprocess.check_output(args, text=True, timeout=120).strip()


def api(path):
    return json.loads(command("gh", "api", path))


def version(root=Path(".")):
    backend = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    web = json.loads((root / "apps/web/package.json").read_text())["version"]
    lock = json.loads((root / "apps/web/package-lock.json").read_text())
    if not VERSION.fullmatch(backend) or {
        backend,
        web,
        lock["version"],
        lock["packages"][""]["version"],
    } != {backend}:
        raise ValueError("Backend, frontend and lockfile must have the same stable release version")
    return "v" + backend


def identity(environ=os.environ):
    repository, sha = environ["GITHUB_REPOSITORY"], environ["GITHUB_SHA"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Expected a full commit SHA")
    return {
        "schema": 1,
        "repository": repository,
        "sha": sha,
        "version": version(),
        "image": "ghcr.io/" + repository.lower(),
    }


def candidate_run(
    repository, sha, request=api, wait_seconds=720, clock=time.monotonic, sleep=time.sleep
):
    deadline = clock() + wait_seconds
    query = urlencode({"event": "push", "branch": "main", "head_sha": sha, "per_page": 100})
    while True:
        runs = request(f"repos/{repository}/actions/workflows/container.yml/runs?{query}")[
            "workflow_runs"
        ]
        eligible = [
            r
            for r in runs
            if r["head_sha"] == sha
            and r["head_branch"] == "main"
            and r["event"] == "push"
            and r["path"] == WORKFLOW
            and r["head_repository"]["full_name"] == repository
        ]
        if not eligible:
            return ""
        run = max(eligible, key=lambda r: r["id"])
        if run["status"] == "completed":
            if run["conclusion"] != "success":
                raise ValueError(
                    "The latest main candidate failed. Repair or rerun it before release."
                )
            artifacts = request(
                f"repos/{repository}/actions/runs/{run['id']}/artifacts?per_page=100"
            )["artifacts"]
            matches = [a for a in artifacts if a["name"] == ARTIFACT and not a["expired"]]
            if not matches:
                # Older workflows or expired artifacts need the complete checks/build path.
                return ""
            if len(matches) != 1 or matches[0]["size_in_bytes"] > 16384:
                raise ValueError("Unexpected candidate artifact")
            return str(run["id"])
        if clock() >= deadline:
            raise ValueError(
                "Main candidate is still running; rerun this release after it completes"
            )
        print(f"Waiting for main candidate run {run['id']}...", flush=True)
        sleep(min(15, max(0, deadline - clock())))


def validate(record, expected):
    if any(record.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "Candidate source, repository, schema or version does not match this release"
        )
    if not isinstance(record.get("digest"), str) or not DIGEST.fullmatch(record["digest"]):
        raise ValueError("Candidate requires an immutable SHA-256 image digest")
    return record


def verify_manifest(manifest, expected_digest=None):
    digest = manifest.get("digest", "")
    if not DIGEST.fullmatch(digest) or (expected_digest and digest != expected_digest):
        raise ValueError("Registry manifest digest does not match candidate")
    platforms = []
    for item in manifest.get("manifests", []):
        if item.get("annotations", {}).get("vnd.docker.reference.type") == "attestation-manifest":
            continue
        platforms.append(
            (item.get("platform", {}).get("os"), item.get("platform", {}).get("architecture"))
        )
    if sorted(platforms) != [("linux", "amd64"), ("linux", "arm64")]:
        raise ValueError("Candidate must contain exactly the tested AMD64 and ARM64 images")
    return digest


def inspect(reference, missing_ok=False):
    result = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", reference, "--format", "{{json .Manifest}}"],
        text=True,
        capture_output=True,
        timeout=120,
    )
    if result.returncode:
        # Only an explicit missing manifest allows a new version. All other errors fail closed.
        if missing_ok and result.stderr.strip() == f"ERROR: {reference}: not found":
            return None
        raise RuntimeError(result.stderr.strip())
    return json.loads(result.stdout)


def channel_tags(ref, release_version, source_sha, main_sha, git_tags):
    if ref == "refs/heads/main":
        return ["edge"] if main_sha == source_sha else []
    if ref != "refs/tags/" + release_version:
        raise ValueError(
            "Release tag must exactly match the package version; only main can publish edge"
        )
    stable = [
        tuple(map(int, tag[1:].split(".")))
        for tag in git_tags
        if tag.startswith("v") and VERSION.fullmatch(tag[1:])
    ]
    current = tuple(map(int, release_version[1:].split(".")))
    return (
        [release_version, "latest"]
        if current >= max(stable, default=current)
        else [release_version]
    )


def promote(record, tags, run=command, read_manifest=inspect):
    source = record["image"] + "@" + record["digest"]
    verify_manifest(read_manifest(source), record["digest"])
    if record["version"] in tags:
        existing = read_manifest(record["image"] + ":" + record["version"], missing_ok=True)
        if existing and existing.get("digest") != record["digest"]:
            raise ValueError("A published version is immutable; choose a new release version")
    for tag in tags:
        run(
            "docker", "buildx", "imagetools", "create", "--tag", record["image"] + ":" + tag, source
        )
        verify_manifest(read_manifest(record["image"] + ":" + tag), record["digest"])


def output(**values):
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def main():
    action = sys.argv[1]
    if action == "version":
        output(version=version())
        return
    expected = identity()
    ref = os.environ["GITHUB_REF"]
    if ref != "refs/heads/main" and ref != "refs/tags/" + expected["version"]:
        raise ValueError("Publication requires main or the tag matching the package version")
    if action == "plan":
        run_id = (
            candidate_run(expected["repository"], expected["sha"])
            if ref.startswith("refs/tags/")
            else ""
        )
        output(**{"reuse-run-id": run_id, "version": expected["version"]})
    elif action == "assemble":
        sources = []
        for arch in ("amd64", "arm64"):
            digest = Path(f"digests/{arch}.txt").read_text().strip()
            if not DIGEST.fullmatch(digest):
                raise ValueError("Invalid architecture digest")
            sources.append(expected["image"] + "@" + digest)
        # Unique per run: simultaneous validation/retries cannot change this reference.
        reference = (
            expected["image"]
            + ":candidate-"
            + os.environ["GITHUB_RUN_ID"]
            + "-"
            + os.environ["GITHUB_RUN_ATTEMPT"]
        )
        command("docker", "buildx", "imagetools", "create", "--tag", reference, *sources)
        expected["digest"] = verify_manifest(inspect(reference))
        Path("candidate").mkdir(exist_ok=True)
        Path("candidate/candidate.json").write_text(json.dumps(expected) + "\n")
    elif action == "publish":
        record = validate(json.loads(Path("candidate/candidate.json").read_text()), expected)
        main_sha = api(f"repos/{expected['repository']}/git/ref/heads/main")["object"]["sha"]
        tags = channel_tags(
            ref,
            expected["version"],
            expected["sha"],
            main_sha,
            command("git", "tag", "--list").splitlines(),
        )
        promote(record, tags)
        channels = ", ".join(tags) or "none (superseded main)"
        print(f"Verified {record['image']}@{record['digest']}; channels: {channels}")
    else:
        raise ValueError("Unknown release operation")


if __name__ == "__main__":
    main()
