"""SABnzbd 3.x/4.x/5.x transport. Submission is not association or completion.

Callers journal dispatch before submit, then reconcile the attempt name, category
and completed folder. This client never changes SABnzbd settings or server paths.
"""

import asyncio
import re
from pathlib import PurePosixPath

import httpx
from pydantic import Field

from app.adapters.contracts import (
    AdapterError,
    Capabilities,
    DownloadState,
    FailureKind,
    SubmissionReceipt,
)
from app.adapters.http import configured_url
from app.adapters.qbittorrent import absolute_path, validate_attempt_tag

MAX_RESPONSE = 2 * 1024 * 1024
MAX_ARTIFACT = 8 * 1024 * 1024


class SabState(DownloadState):
    category: str
    names: set[str] = Field(default_factory=set)
    failed: bool = False
    reported_complete: bool = False


def inside(path, root):
    try:
        relative = PurePosixPath(path).relative_to(root)
    except ValueError:
        return None
    return "" if str(relative) == "." else str(relative)


def categories_match(actual, expected):
    actual, expected = actual.strip(), expected.strip()
    if expected == "":
        return actual in {"", "*", "Default"}
    return actual == expected


def verify_association(states: list[SabState], *, tag: str, save_path: str, category: str):
    """None means not observed. An uncertain match is never permission to add again."""
    tag, save_path = validate_attempt_tag(tag), absolute_path(save_path)
    if not re.fullmatch(r"[A-Za-z0-9_-]{0,100}", category):
        raise ValueError("Use one simple download category")
    if not states:
        return None
    if len(states) != 1:
        raise AdapterError(FailureKind.UNCERTAIN, "More than one SABnzbd job matches this attempt.")
    state = states[0]
    if tag not in state.names or not categories_match(state.category, category):
        raise AdapterError(
            FailureKind.UNCERTAIN,
            "The existing SABnzbd job does not match the recorded attempt.",
        )
    if state.completed and inside(state.save_path, save_path) in {None, ""}:
        raise AdapterError(
            FailureKind.UNCERTAIN,
            "The completed SABnzbd folder is outside the saved download path.",
        )
    return state.model_copy(update={"association_verified": True})


def nzo_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value)


def associated_name(row):
    nzb_name = row.get("nzb_name")
    if isinstance(nzb_name, str) and nzb_name:
        return nzb_name
    filename = row.get("filename")
    return filename if isinstance(filename, str) else ""


def job_names(row):
    name = associated_name(row)
    if not name:
        return set()
    names = {name}
    if name.lower().endswith(".nzb"):
        names.add(name[:-4])
    return names


def slots(payload, key):
    section = payload.get(key)
    if not isinstance(section, dict):
        raise AdapterError(FailureKind.PARSER, "SABnzbd returned an unreadable job list.")
    rows = section.get("slots")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise AdapterError(FailureKind.PARSER, "SABnzbd returned an unreadable job list.")
    if len(rows) > 20:
        raise AdapterError(
            FailureKind.UNAVAILABLE,
            "SABnzbd returned too many jobs to confirm this attempt.",
        )
    return rows


def parse_job(row, *, completed):
    try:
        identifier = row["nzo_id"]
        if not nzo_id(identifier):
            raise ValueError("Invalid job id")
        names = job_names(row)
        if not names:
            raise ValueError("Missing job name")
        category = row.get("category", row.get("cat", ""))
        if not isinstance(category, str):
            raise ValueError("Invalid category")
        status = row.get("status")
        if not isinstance(status, str) or len(status) > 40:
            raise ValueError("Invalid status")
        failed = status.lower() in {"failed", "aborted"}
        storage = row.get("storage") or ""
        if completed and not failed:
            path = absolute_path(storage)
        else:
            path = "/pending"
        return SabState(
            external_id=identifier,
            state=status,
            completed=completed and not failed,
            save_path=path,
            category=category,
            names=names,
            failed=failed,
            reported_complete=completed and not failed,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise AdapterError(
            FailureKind.PARSER, "SABnzbd returned incomplete job evidence."
        ) from error


class SabClient:
    def __init__(self, base_url: str, api_key: str, *, transport=None):
        if (
            not isinstance(api_key, str)
            or not api_key
            or any(ord(character) < 33 or ord(character) > 126 for character in api_key)
        ):
            raise AdapterError(FailureKind.AUTHENTICATION, "SABnzbd API key is missing.")
        self._api_key = api_key
        self._capabilities = None
        self.client = httpx.AsyncClient(
            base_url=configured_url(base_url) + "/",
            headers={"X-Api-Key": api_key, "Accept": "application/json"},
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(40, connect=10),
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def _request(self, mode, *, params=None, files=None, mutating=False):
        query = {"mode": mode, "output": "json", **(params or {})}
        uncertain = FailureKind.UNCERTAIN if mutating else FailureKind.PARSER
        try:
            async with asyncio.timeout(50):
                response = await (
                    self.client.post("api", params=query, files=files)
                    if files
                    else self.client.get("api", params=query)
                )
        except (httpx.HTTPError, TimeoutError) as error:
            raise AdapterError(
                FailureKind.UNCERTAIN if mutating else FailureKind.ROUTE,
                "SABnzbd submission outcome is unknown. Reconcile before submitting again."
                if mutating
                else "SABnzbd could not be reached.",
            ) from error
        if response.status_code in {401, 403}:
            raise AdapterError(FailureKind.AUTHENTICATION, "SABnzbd rejected the API key.")
        if 300 <= response.status_code < 400:
            raise AdapterError(
                uncertain if mutating else FailureKind.ROUTE,
                "SABnzbd redirected the request.",
            )
        if not 200 <= response.status_code < 300:
            raise AdapterError(
                uncertain if mutating else FailureKind.UNAVAILABLE,
                "SABnzbd could not complete this request.",
            )
        if len(response.content) > MAX_RESPONSE:
            raise AdapterError(uncertain, "SABnzbd response exceeded the size limit.")
        try:
            payload = response.json()
        except ValueError as error:
            raise AdapterError(uncertain, "SABnzbd returned an unreadable response.") from error
        if not isinstance(payload, dict):
            raise AdapterError(uncertain, "SABnzbd returned an unreadable response.")
        error = payload.get("error")
        if isinstance(error, str) and error:
            if "api key" in error.casefold() or "apikey" in error.casefold():
                raise AdapterError(FailureKind.AUTHENTICATION, "SABnzbd rejected the API key.")
            raise AdapterError(
                uncertain if mutating else FailureKind.UNAVAILABLE,
                "SABnzbd could not complete this request.",
            )
        return payload

    async def capabilities(self) -> Capabilities:
        if self._capabilities:
            return self._capabilities
        payload = await self._request("version")
        version = payload.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[345]\.\d+\.\d+", version):
            raise AdapterError(
                FailureKind.UNSUPPORTED, "This adapter requires SABnzbd 3.x, 4.x, or 5.x."
            )
        self._capabilities = Capabilities(
            version=version,
            operations={"submit", "find", "status"},
            protocols={"nzb"},
            limitations=["POSIX complete folders; SABnzbd chooses the final folder name."],
        )
        return self._capabilities

    async def download_location(self, category: str) -> str:
        """Read the category folder without changing SABnzbd configuration."""
        await self.capabilities()
        misc = await self._request("get_config", params={"section": "misc"})
        categories = await self._request("get_config", params={"section": "categories"})
        try:
            complete = absolute_path(misc["config"]["misc"]["complete_dir"])
            rows = categories["config"]["categories"]
            if not isinstance(rows, list):
                raise ValueError("Invalid categories")
            chosen = complete
            for row in rows:
                if not isinstance(row, dict) or row.get("name") != category:
                    continue
                folder = row.get("dir") or ""
                if not isinstance(folder, str) or not folder:
                    break
                chosen = absolute_path(
                    folder if folder.startswith("/") else complete + "/" + folder.strip("/")
                )
                break
            return chosen
        except (KeyError, TypeError, ValueError) as error:
            raise AdapterError(
                FailureKind.PARSER, "SABnzbd returned an invalid download location."
            ) from error

    async def _matching(self, tag):
        queue = slots(
            await self._request("queue", params={"search": tag, "start": 0, "limit": 20}),
            "queue",
        )
        history = slots(
            await self._request("history", params={"search": tag, "start": 0, "limit": 20}),
            "history",
        )
        found = {}
        for row in queue:
            if tag in job_names(row):
                state = parse_job(row, completed=False)
                found[state.external_id] = state
        for row in history:
            if tag in job_names(row):
                state = parse_job(row, completed=True)
                found[state.external_id] = state
        return list(found.values())

    async def find(self, *, attempt_tag: str, torrent_hash: str | None) -> list[SabState]:
        del torrent_hash
        tag = validate_attempt_tag(attempt_tag)
        await self.capabilities()
        return await self._matching(tag)

    async def status(self, external_id: str) -> SabState:
        if not nzo_id(external_id):
            raise AdapterError(FailureKind.PARSER, "SABnzbd job id is invalid.")
        await self.capabilities()
        queue = slots(
            await self._request("queue", params={"nzo_ids": external_id, "limit": 5}),
            "queue",
        )
        history = slots(
            await self._request("history", params={"search": external_id, "start": 0, "limit": 20}),
            "history",
        )
        matches = [
            parse_job(row, completed=False) for row in queue if row.get("nzo_id") == external_id
        ]
        matches += [
            parse_job(row, completed=True) for row in history if row.get("nzo_id") == external_id
        ]
        unique = {state.external_id: state for state in matches}
        if not unique:
            raise AdapterError(
                FailureKind.NOT_FOUND, "The recorded SABnzbd job is no longer visible."
            )
        if len(unique) != 1:
            raise AdapterError(FailureKind.PARSER, "SABnzbd returned multiple job records.")
        return next(iter(unique.values()))

    async def submit(
        self,
        artifact: bytes | str,
        *,
        attempt_tag: str,
        save_path: str,
        category: str = "",
    ) -> SubmissionReceipt:
        tag = validate_attempt_tag(attempt_tag)
        absolute_path(save_path)
        if not re.fullmatch(r"[A-Za-z0-9_-]{0,100}", category):
            raise ValueError("Use one simple download category")
        if not isinstance(artifact, bytes) or not 0 < len(artifact) <= MAX_ARTIFACT:
            raise ValueError("Expected one bounded NZB artifact")
        await self.capabilities()
        params = {"nzbname": tag}
        if category:
            params["cat"] = category
        payload = await self._request(
            "addfile",
            params=params,
            files={"name": ("book.nzb", artifact, "application/x-nzb")},
            mutating=True,
        )
        identifiers = payload.get("nzo_ids")
        if (
            payload.get("status") is True
            and isinstance(identifiers, list)
            and len(identifiers) == 1
            and nzo_id(identifiers[0])
        ):
            return SubmissionReceipt(external_ids=[identifiers[0]])
        raise AdapterError(
            FailureKind.UNCERTAIN,
            "SABnzbd did not confirm submission. Reconcile before submitting again.",
        )
