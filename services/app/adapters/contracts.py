from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field


class FailureKind(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSION = "permission"
    RATE_LIMIT = "rate_limit"
    ROUTE = "route"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    PARSER = "parser"
    UNSUPPORTED = "unsupported"
    UNCERTAIN = "uncertain"
    NOT_FOUND = "not_found"


class AdapterError(Exception):
    """Safe error text only. Raw responses and credential-bearing URLs stay out."""

    def __init__(self, kind: FailureKind, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after
        self.proxy_retryable = False


class MutationError(AdapterError):
    """Whether reconciliation is required before retrying an external write."""

    def __init__(self, kind, message, *, may_have_applied, retry_after=None):
        super().__init__(kind, message, retry_after=retry_after)
        self.may_have_applied = may_have_applied


class ResponseTooLarge(AdapterError):
    """A decoded response exceeded its work budget, not a library-count ceiling."""

    def __init__(self, limit: int, received: int, label: str = "Server response"):
        super().__init__(
            FailureKind.PARSER,
            f"{label} exceeded the response byte budget ({limit} bytes; "
            f"received at least {received} bytes).",
        )
        self.limit = limit
        self.received = received


class Capabilities(BaseModel):
    version: str | None = None
    operations: set[str] = Field(default_factory=set)
    protocols: set[str] = Field(default_factory=set)
    limitations: list[str] = Field(default_factory=list)


class ProviderReference(BaseModel):
    provider: str
    kind: str
    external_id: str


class CatalogCandidate(BaseModel):
    reference: ProviderReference
    title: str
    authors: list[str] = Field(default_factory=list)
    language: str | None = None
    description: str | None = None
    cover_url: str | None = None
    identifiers: dict[str, list[str]] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)


class CatalogPage(BaseModel):
    items: list[CatalogCandidate]
    cursor: str | None = None
    complete: bool = False


class CoverageClaim(BaseModel):
    title: str
    authors: list[str] = Field(default_factory=list)
    work_id: UUID | None = None
    version_id: UUID | None = None
    evidence: Literal["claimed", "corroborated", "verified"] = "claimed"


class Release(BaseModel):
    source: str
    source_id: str
    indexer_id: str | None = None
    raw_title: str
    medium: Literal["ebook", "audio"] | None = None
    authors: list[str] = Field(default_factory=list)
    narrators: list[str] = Field(default_factory=list)
    language: str | None = None
    formats: list[str] = Field(default_factory=list)
    size_bytes: int | None = Field(default=None, ge=0)
    seeders: int | None = Field(default=None, ge=0)
    description: str | None = None
    coverage: list[CoverageClaim] = Field(default_factory=list)
    protocol: Literal["torrent", "nzb", "direct", "soulseek", "unknown"] = "unknown"
    details: dict[str, Any] = Field(default_factory=dict)


class DownloadFile(BaseModel):
    relative_path: str
    size_bytes: int = Field(ge=0)
    complete: bool


class DownloadState(BaseModel):
    external_id: str
    state: str
    completed: bool
    save_path: str
    files: list[DownloadFile] = Field(default_factory=list)
    association_verified: bool = False


class SubmissionReceipt(BaseModel):
    """Transport acknowledgement; identifiers are hints until independently reconciled."""

    external_ids: list[str] = Field(default_factory=list)
    pending: bool = False


class InventoryPage(BaseModel):
    items: list[dict[str, Any]]
    cursor: str | None = None
    complete: bool = False


class MetadataProvider(Protocol):
    async def capabilities(self) -> Capabilities: ...
    async def search(self, query: str, cursor: str | None = None) -> CatalogPage: ...
    async def fetch(self, reference: ProviderReference) -> CatalogCandidate: ...


class ReleaseSource(Protocol):
    async def capabilities(self) -> Capabilities: ...
    async def search(self, query: str, medium: str | None = None) -> list[Release]: ...
    async def detail(self, source_id: str) -> Release: ...


class LibraryBackend(Protocol):
    async def capabilities(self) -> Capabilities: ...
    async def libraries(self) -> list[dict[str, Any]]: ...
    async def inventory(self, library_id: str, cursor: str | None = None) -> InventoryPage: ...
    async def item(self, item_id: str) -> dict[str, Any]: ...
    async def scan(self, library_id: str) -> None: ...


class DownloadClient(Protocol):
    async def capabilities(self) -> Capabilities: ...
    async def find(self, *, attempt_tag: str, torrent_hash: str | None) -> list[DownloadState]: ...
    async def status(self, external_id: str) -> DownloadState: ...
    async def submit(
        self, artifact: bytes | str, *, attempt_tag: str, save_path: str
    ) -> SubmissionReceipt: ...
