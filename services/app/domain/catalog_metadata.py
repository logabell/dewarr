from datetime import UTC, datetime
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select

from app.adapters.catalog_types import CATALOG_PROVIDERS, BookData, Provider
from app.db.models import (
    ListCatalogBinding,
    MetadataSettings,
    ProviderObject,
    User,
    Version,
    Work,
    WorkMetadataSource,
)
from app.domain.identity import normalized, work_key
from app.domain.operations import transaction_lock
from app.domain.visibility import visible_origin_work, visible_work
from app.domain.work_graph import canonical_work, family_ids

FIELDS = ("title", "authors", "description", "publication_year", "language", "cover_url")


class MetadataPreferences(BaseModel):
    primary: Provider = "hardcover"
    automatic_enrichment: bool = True
    automatic_edition_lookup: bool = True
    # After each library sync, save unique verified Hardcover matches for library books.
    automatic_library_matching: bool = True
    # Fill in the series on Audiobookshelf items that have none, after an automatic match.
    write_library_series: bool = False
    # Once every part of a recording is in an Audiobookshelf library, fold the part folders
    # into one book with Disc N folders.
    combine_library_parts: bool = True
    language: str = Field(default="en", min_length=2, max_length=20)
    filter_language: bool = False
    covers: Literal["automatic", "hardcover", "openlibrary"] = "automatic"
    field_providers: dict[str, Provider] = Field(default_factory=dict)

    @field_validator("field_providers")
    @classmethod
    def supported(cls, value):
        if set(value) - set(FIELDS):
            raise ValueError("Unsupported metadata field")
        return value


async def preferences(db):
    row = await db.get(MetadataSettings, 1)
    return MetadataPreferences.model_validate(row.preferences if row else {})


def same_work(work, book):
    key = work_key(book.title, book.authors)
    return bool(key and key == work_key(work.title, work.authors))


async def resolve_fields(db, work, settings):
    sources = (
        await db.scalars(
            select(WorkMetadataSource)
            .join(Work, Work.id == WorkMetadataSource.work_id)
            .where(
                WorkMetadataSource.work_id.in_(family_ids(work.id)),
                WorkMetadataSource.provider.in_(CATALOG_PROVIDERS),
                WorkMetadataSource.accepted.is_(True),
                Work.catalog_public.is_(True) if work.catalog_public else True,
            )
            .order_by(WorkMetadataSource.fetched_at.desc(), WorkMetadataSource.id)
        )
    ).all()
    fields = dict(work.metadata_fields.get("fields", {}))
    for field in FIELDS:
        previous = fields.get(field, {})
        if previous.get("locked"):
            continue
        primary = settings.field_providers.get(field) or (
            settings.covers
            if field == "cover_url" and settings.covers != "automatic"
            else settings.primary
        )
        ordered = sorted(sources, key=lambda source: source.provider != primary)
        selected = next(
            (source for source in ordered if source.snapshot.get(field) not in (None, "", [])), None
        )
        if selected:
            value = selected.snapshot[field]
            setattr(work, field, value)
            fields[field] = {
                "value": value,
                "provider": selected.provider,
                "external_id": selected.external_id,
                "locked": False,
                "reason": "Preferred provider"
                if selected.provider == primary
                else "Filled missing field",
                "observed_at": selected.fetched_at.isoformat(),
            }
    work.metadata_fields = {**work.metadata_fields, "fields": fields}
    uncertain = any(
        fields.get(name, {}).get("provider") == "unmatched" for name in ("title", "authors")
    )
    work.metadata_fields = {**work.metadata_fields, "identity_rejected": uncertain}
    work.match_key = None if uncertain else work_key(work.title, work.authors)


async def attach_source(db, work, book, *, explicit=False, verified_match=False):
    work = await db.get(Work, work.id, with_for_update=True, populate_existing=True)
    if work.redirect_to:
        raise HTTPException(
            409, "This book was merged during lookup. Retry with its current record."
        )
    link = await db.scalar(
        select(WorkMetadataSource).where(
            WorkMetadataSource.work_id.in_(family_ids(work.id)),
            WorkMetadataSource.provider == book.provider,
            WorkMetadataSource.external_id == book.external_id,
        )
    )
    if link and not link.accepted and not explicit:
        raise HTTPException(
            409,
            "This catalog source was explicitly unmatched. Confirm a new match to use it again.",
        )
    if link and not explicit:
        try:
            old = BookData.model_validate(link.snapshot)
        except ValidationError:
            old = None
        if old is not None and (
            work_key(old.title, old.authors) != work_key(book.title, book.authors)
            or (book.canonical_id and book.canonical_id != book.external_id)
        ):
            raise HTTPException(
                409,
                "The provider changed this book's identity. Review the match before refreshing.",
            )
    elif not link and not explicit and not verified_match and not same_work(work, book):
        raise HTTPException(409, "This catalog result needs an explicit match confirmation.")
    if book.canonical_id and book.canonical_id != book.external_id:
        raise HTTPException(
            409, "This provider record was merged. Select its current catalog record."
        )
    if not link:
        link = WorkMetadataSource(
            work_id=work.id, provider=book.provider, external_id=book.external_id
        )
        db.add(link)
    snapshot = book.model_dump(mode="json")
    if book.editions_offset:
        if not link.snapshot or link.snapshot.get("next_edition_offset") != book.editions_offset:
            raise HTTPException(409, "Edition pagination changed. Refresh this book and retry.")
        try:
            previous = BookData.model_validate(link.snapshot)
        except ValidationError as error:
            raise HTTPException(409, "Refresh this book before loading more editions.") from error
        if work_key(previous.title, previous.authors) != work_key(book.title, book.authors):
            raise HTTPException(
                409, "The provider changed this book. Refresh before loading more editions."
            )
        snapshot = {**link.snapshot, "editions_more": book.editions_more}
    snapshot["next_edition_offset"] = book.editions_offset + 50 if book.editions_more else None
    link.snapshot, link.fetched_at, link.accepted = (
        snapshot,
        datetime.now(UTC),
        True,
    )
    link.manual_match = explicit or bool(link.manual_match)
    await db.flush()
    await resolve_fields(db, work, await preferences(db))
    work.provisional = False
    for edition in book.editions:
        # Per-work namespace prevents attaching a private library version to an unrelated catalog.
        namespace = f"{book.provider}:{link.work_id}"
        version_link = await db.scalar(
            select(ProviderObject).where(
                ProviderObject.provider == namespace,
                ProviderObject.kind == "edition",
                ProviderObject.external_id == edition.external_id,
            )
        )
        snapshot = edition.model_dump(mode="json")
        if version_link:
            if version_link.metadata_source_id and version_link.metadata_source_id != link.id:
                previous_source = await db.get(WorkMetadataSource, version_link.metadata_source_id)
                if not explicit or (previous_source and previous_source.accepted):
                    raise HTTPException(
                        409,
                        "An edition identifier conflicts with another catalog source. "
                        "Review those matches first.",
                    )
            version_link.metadata_source_id = link.id
            if version_link.manual_lock:
                continue
            identity_fields = (
                "medium",
                "title",
                "language",
                "narrators",
                "publication_year",
                "identifiers",
                "abridged",
            )
            if any(
                version_link.snapshot.get(field) != snapshot.get(field) for field in identity_fields
            ):
                # Existing assets keep their identity; review contradictory version changes.
                version_link.match_status = "needs-review"
                version_link.pending_snapshot = snapshot
            else:
                version_link.snapshot = snapshot
                version_link.match_status = "matched"
                version_link.pending_snapshot = None
            continue
        version = Version(
            work_id=link.work_id,
            medium=edition.medium,
            title=edition.title,
            language=edition.language,
            narrators=edition.narrators,
            abridged=edition.abridged,
            publication_year=edition.publication_year,
            identifiers=edition.identifiers,
        )
        db.add(version)
        await db.flush()
        db.add(
            ProviderObject(
                provider=namespace,
                kind="edition",
                external_id=edition.external_id,
                work_id=link.work_id,
                version_id=version.id,
                snapshot=snapshot,
                match_status="matched",
                metadata_source_id=link.id,
            )
        )


async def import_book(db, user, book):
    # Take the actor fence before the shared list/catalog identity lock. Otherwise
    # an audit FK can deadlock against a list worker holding the actor row.
    user = await db.scalar(
        select(User)
        .where(User.id == user.id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if not user or not user.active or user.role == "viewer":
        raise HTTPException(403, "Catalog editing is no longer permitted")
    await transaction_lock(db, f"goodreads:catalog:{user.id}")
    await transaction_lock(db, "catalog:" + book.provider + ":" + book.external_id)
    await transaction_lock(db, "identity:" + normalized(book.title))
    rejected = await db.scalar(
        select(WorkMetadataSource.id)
        .join(Work)
        .where(
            WorkMetadataSource.provider == book.provider,
            WorkMetadataSource.external_id == book.external_id,
            WorkMetadataSource.accepted.is_(False),
            visible_origin_work(user),
        )
        .limit(1)
    )
    linked = (
        await db.scalars(
            select(Work)
            .join(WorkMetadataSource)
            .where(
                WorkMetadataSource.provider == book.provider,
                WorkMetadataSource.external_id == book.external_id,
                WorkMetadataSource.accepted.is_(True),
                visible_origin_work(user),
            )
        )
    ).all()
    linked = list(
        {work.id: work for work in [await canonical_work(db, row.id) for row in linked]}.values()
    )
    if len(linked) > 1:
        raise HTTPException(
            409, "Multiple catalog records need reconciliation. Open the intended book to match it."
        )
    if linked:
        # Adding an already catalogued title is idempotent, not an implicit refresh.
        return linked[0]
    else:
        if rejected:
            raise HTTPException(
                409,
                "This catalog record was previously unmatched. "
                "Confirm its intended book before adding it again.",
            )
        key = work_key(book.title, book.authors)
        matches = (
            (
                await db.scalars(
                    select(Work).where(
                        Work.match_key == key, visible_work(user), Work.redirect_to.is_(None)
                    )
                )
            ).all()
            if key
            else []
        )
        binding = await db.scalar(
            select(ListCatalogBinding).where(
                ListCatalogBinding.owner_id == user.id,
                ListCatalogBinding.identity_key == f"{book.provider}:{book.external_id}",
            )
        )
        if binding:
            bound = await canonical_work(db, binding.work_id)
            if await db.scalar(select(Work.id).where(Work.id == bound.id, visible_work(user))):
                if not same_work(bound, book):
                    raise HTTPException(
                        409,
                        "The followed book differs from this catalog result; "
                        "review its identity first",
                    )
                matches = [bound]
        if len(matches) > 1:
            raise HTTPException(
                409, "Multiple books match. Choose the intended book from your catalog."
            )
        work = (
            matches[0]
            if matches
            else Work(title=book.title, authors=book.authors, catalog_public=True, match_key=key)
        )
        if not matches:
            db.add(work)
            await db.flush()
        else:
            work = await db.get(Work, work.id, with_for_update=True, populate_existing=True)
            if work.redirect_to:
                raise HTTPException(
                    409, "This book was merged during lookup. Retry the catalog import."
                )
    # Matching private inventory never promotes its metadata to a public catalog record.
    await attach_source(db, work, book, explicit=not matches)
    return work
