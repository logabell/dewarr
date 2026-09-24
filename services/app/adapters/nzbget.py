"""NZBGet 21+ JSON-RPC transport. Submission is not association or completion.

Callers journal dispatch before append, then reconcile the duplicate key, category
and completed folder. This client never changes NZBGet settings or server paths.
"""

import asyncio
import base64
import re
from pathlib import PurePosixPath

import httpx

from app.adapters.contracts import (
    AdapterError,
    Capabilities,
    DownloadState,
    FailureKind,
    SubmissionReceipt,
)
from app.adapters.http import configured_url
from app.adapters.qbittorrent import absolute_path, validate_attempt_tag

MAX_RESPONSE = 8 * 1024 * 1024
MAX_HISTORY = 32 * 1024 * 1024
MAX_ARTIFACT = 8 * 1024 * 1024
VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.\d+)?(?:[-+][A-Za-z0-9._-]+)?$")


class NzbState(DownloadState):
    category: str
    dupe_key: str
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
        return actual == ""
    return actual == expected


def verify_association(states: list[NzbState], *, tag: str, save_path: str, category: str):
    """None means not observed. An uncertain match is never permission to add again."""
    tag, save_path = validate_attempt_tag(tag), absolute_path(save_path)
    if not re.fullmatch(r"[A-Za-z0-9_-]{0,100}", category):
        raise ValueError("Use one simple download category")
    if not states:
        return None
    if len(states) != 1:
        raise AdapterError(FailureKind.UNCERTAIN, "More than one NZBGet job matches this attempt.")
    state = states[0]
    if state.dupe_key != tag or not categories_match(state.category, category):
        raise AdapterError(
            FailureKind.UNCERTAIN,
            "The existing NZBGet job does not match the recorded attempt.",
        )
    if state.completed and inside(state.save_path, save_path) in {None, ""}:
        raise AdapterError(
            FailureKind.UNCERTAIN,
            "The completed NZBGet folder is outside the saved download path.",
        )
    return state.model_copy(update={"association_verified": True})


def nzb_id(value):
    return isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,11}", value)


def credential(value, label):
    invalid = not isinstance(value, str) or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    )
    if invalid:
        raise AdapterError(FailureKind.AUTHENTICATION, f"NZBGet {label} is invalid.")
    return value


def groups(payload):
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise AdapterError(FailureKind.PARSER, "NZBGet returned an unreadable job list.")
    return payload


def associated(row, tag):
    return row.get("Kind") == "NZB" and row.get("DupeKey") == tag


def completed_status(status):
    return status.startswith("SUCCESS/") or status == "WARNING/SCRIPT"


def output_folder(row):
    final = row.get("FinalDir") or ""
    dest = row.get("DestDir") or ""
    chosen = final if isinstance(final, str) and final.strip() else dest
    if not isinstance(chosen, str):
        raise ValueError("Invalid destination")
    return absolute_path(chosen)


def parse_group(row, *, completed):
    try:
        identifier = row.get("NZBID")
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or not 0 < identifier <= 2_000_000_000
        ):
            raise ValueError("Invalid job id")
        category = row.get("Category", "")
        if not isinstance(category, str):
            raise ValueError("Invalid category")
        status = row.get("Status")
        if not isinstance(status, str) or not status or len(status) > 40:
            raise ValueError("Invalid status")
        key = row.get("DupeKey")
        if not isinstance(key, str) or not key:
            raise ValueError("Missing duplicate key")
        failed = completed and not completed_status(status)
        path = output_folder(row) if completed and not failed else "/pending"
        return NzbState(
            external_id=str(identifier),
            state=status,
            completed=completed and not failed,
            save_path=path,
            category=category,
            dupe_key=key,
            failed=failed,
            reported_complete=completed and not failed,
        )
    except (TypeError, ValueError) as error:
        raise AdapterError(
            FailureKind.PARSER, "NZBGet returned incomplete job evidence."
        ) from error


def option_map(payload):
    if not isinstance(payload, list):
        raise AdapterError(FailureKind.PARSER, "NZBGet returned an unreadable configuration.")
    options = {}
    for row in payload:
        if not isinstance(row, dict):
            raise AdapterError(FailureKind.PARSER, "NZBGet returned an unreadable configuration.")
        name, value = row.get("Name"), row.get("Value")
        if isinstance(name, str) and isinstance(value, str):
            options[name] = value
    return options


def category_folder(options, category):
    try:
        root = absolute_path(options["DestDir"])
    except (KeyError, ValueError) as error:
        raise AdapterError(
            FailureKind.PARSER, "NZBGet returned an invalid download location."
        ) from error
    if not category:
        return root
    for index in range(1, 100):
        name = options.get(f"Category{index}.Name")
        if name is None:
            break
        if name != category:
            continue
        folder = options.get(f"Category{index}.DestDir") or ""
        if not folder:
            return (
                absolute_path(str(PurePosixPath(root) / category))
                if options.get("AppendCategoryDir", "yes").casefold() == "yes"
                else root
            )
        try:
            chosen = (
                folder if folder.startswith("/") else str(PurePosixPath(root) / folder.strip("/"))
            )
            return absolute_path(chosen)
        except ValueError as error:
            raise AdapterError(
                FailureKind.PARSER, "NZBGet returned an invalid download location."
            ) from error
    raise AdapterError(
        FailureKind.NOT_FOUND,
        f'The NZBGet category "{category}" does not exist.',
    )


class NzbClient:
    def __init__(self, base_url: str, username: str, password: str, *, transport=None):
        self._username = credential(username, "username")
        self._password = credential(password, "password")
        self._capabilities = None
        auth = (
            httpx.BasicAuth(self._username, self._password)
            if self._username or self._password
            else None
        )
        self.client = httpx.AsyncClient(
            base_url=configured_url(base_url) + "/",
            auth=auth,
            headers={"Accept": "application/json"},
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(40, connect=10),
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.client.aclose()

    async def _rpc(self, method, params=None, *, mutating=False, limit=MAX_RESPONSE):
        uncertain = FailureKind.UNCERTAIN if mutating else FailureKind.PARSER
        body = {"jsonrpc": "2.0", "method": method, "params": list(params or []), "id": 1}
        try:
            async with asyncio.timeout(50):
                response = await self.client.post("jsonrpc", json=body)
        except (httpx.HTTPError, TimeoutError) as error:
            raise AdapterError(
                FailureKind.UNCERTAIN if mutating else FailureKind.ROUTE,
                "NZBGet submission outcome is unknown. Reconcile before submitting again."
                if mutating
                else "NZBGet could not be reached.",
            ) from error
        if response.status_code in {401, 403}:
            raise AdapterError(FailureKind.AUTHENTICATION, "NZBGet rejected the credentials.")
        if 300 <= response.status_code < 400:
            raise AdapterError(
                uncertain if mutating else FailureKind.ROUTE,
                "NZBGet redirected the request.",
            )
        if not 200 <= response.status_code < 300:
            raise AdapterError(
                uncertain if mutating else FailureKind.UNAVAILABLE,
                "NZBGet could not complete this request.",
            )
        if len(response.content) > limit:
            raise AdapterError(
                FailureKind.UNCERTAIN if mutating else FailureKind.UNAVAILABLE,
                "NZBGet response exceeded the size limit.",
            )
        try:
            payload = response.json()
        except ValueError as error:
            raise AdapterError(uncertain, "NZBGet returned an unreadable response.") from error
        if not isinstance(payload, dict) or ("result" not in payload and "error" not in payload):
            raise AdapterError(uncertain, "NZBGet returned an unreadable response.")
        if payload.get("error"):
            raise AdapterError(
                uncertain if mutating else FailureKind.UNAVAILABLE,
                "NZBGet could not complete this request.",
            )
        return payload["result"]

    async def capabilities(self) -> Capabilities:
        if self._capabilities:
            return self._capabilities
        version = await self._rpc("version")
        match = VERSION.fullmatch(version) if isinstance(version, str) else None
        if not match or int(match.group(1)) < 21:
            raise AdapterError(FailureKind.UNSUPPORTED, "This adapter requires NZBGet 21 or newer.")
        self._capabilities = Capabilities(
            version=version,
            operations={"submit", "find", "status"},
            protocols={"nzb"},
            limitations=["POSIX complete folders; NZBGet chooses the final folder name."],
        )
        return self._capabilities

    async def download_location(self, category: str) -> str:
        """Read the category folder without changing NZBGet configuration."""
        await self.capabilities()
        if not re.fullmatch(r"[A-Za-z0-9_-]{0,100}", category):
            raise ValueError("Use one simple download category")
        options = option_map(await self._rpc("config"))
        if options.get("KeepHistory") == "0":
            raise AdapterError(
                FailureKind.UNSUPPORTED,
                "NZBGet must keep download history so completed jobs can be reconciled.",
            )
        return category_folder(options, category)

    async def _matching(self, tag):
        queue = groups(await self._rpc("listgroups", [0]))
        found = {}
        for row in queue:
            if associated(row, tag):
                state = parse_group(row, completed=False)
                found[state.external_id] = state
        # History is the whole NZBGet log and can be large. A queued match is
        # enough; reading history must not hide a download that is still running.
        if found:
            return list(found.values())
        history = groups(await self._rpc("history", [False], limit=MAX_HISTORY))
        for row in history:
            if not associated(row, tag):
                continue
            state = parse_group(row, completed=True)
            if state.external_id in found:
                raise AdapterError(FailureKind.PARSER, "NZBGet returned conflicting job records.")
            found[state.external_id] = state
        return list(found.values())

    async def find(self, *, attempt_tag: str, torrent_hash: str | None) -> list[NzbState]:
        del torrent_hash
        tag = validate_attempt_tag(attempt_tag)
        await self.capabilities()
        return await self._matching(tag)

    async def status(self, external_id: str) -> NzbState:
        if not nzb_id(external_id):
            raise AdapterError(FailureKind.PARSER, "NZBGet job id is invalid.")
        await self.capabilities()
        target = int(external_id)
        matches = []
        for row in groups(await self._rpc("listgroups", [0])):
            if row.get("NZBID") == target and row.get("Kind") == "NZB":
                matches.append(parse_group(row, completed=False))
        if len(matches) > 1:
            raise AdapterError(FailureKind.PARSER, "NZBGet returned conflicting job records.")
        if matches:
            return matches[0]
        for row in groups(await self._rpc("history", [False], limit=MAX_HISTORY)):
            if row.get("NZBID") == target and row.get("Kind") == "NZB":
                matches.append(parse_group(row, completed=True))
        unique = {state.external_id: state for state in matches}
        if not unique:
            raise AdapterError(
                FailureKind.NOT_FOUND, "The recorded NZBGet job is no longer visible."
            )
        if len(matches) != 1:
            raise AdapterError(FailureKind.PARSER, "NZBGet returned conflicting job records.")
        return matches[0]

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
        result = await self._rpc(
            "append",
            [
                "book.nzb",
                base64.standard_b64encode(artifact).decode("ascii"),
                category,
                0,
                False,
                False,
                tag,
                0,
                "FORCE",
            ],
            mutating=True,
        )
        if isinstance(result, int) and not isinstance(result, bool) and 0 < result <= 2_000_000_000:
            return SubmissionReceipt(external_ids=[str(result)])
        raise AdapterError(
            FailureKind.UNCERTAIN,
            "NZBGet did not confirm submission. Reconcile before submitting again.",
        )
