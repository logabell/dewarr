"""Public discovery snapshots, reader layouts and independent display/tracking controls."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from app.adapters.contracts import AdapterError
from app.adapters.goodreads_discovery import CollectionBook, fetch_collection, image_url, source
from app.api.catalog import WorkView, work_view
from app.api.dependencies import CurrentUser, Database, Member
from app.api.metadata import adapter_http_error, current_actor
from app.db.models import DiscoveryFollow, DiscoveryLayout, ProviderCache
from app.domain.availability import availability_for
from app.domain.catalog_bindings import displayed_provider_works
from app.domain.discovery_catalog import (
    catalog,
    coverage,
    live_catalog,
    save_snapshot,
    snapshot_key,
)
from app.domain.discovery_pages import collection_page, find_book
from app.domain.hardcover_matching import MatchResult
from app.domain.operations import transaction_lock

router = APIRouter(prefix="/discovery", tags=["discovery-collections"])


class DiscoveryPreferences(BaseModel):
    hidden: list[str] = Field(default_factory=list, max_length=200)
    order: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("hidden", "order")
    @classmethod
    def valid_keys(cls, value):
        if any(len(v) > 250 for v in value) or len(set(value)) != len(value):
            raise ValueError("Use unique shelf identifiers")
        return value


class CollectionCard(BaseModel):
    id: str
    kind: Literal["award", "listopia", "chart"]
    provider: str = "goodreads"
    description: str | None = None
    audience: str = "all"
    language: str | None = None
    medium: str | None = None
    region: str | None = None
    edition_date: str | None = None
    refresh_mode: Literal["live", "app-update"] = "live"
    title: str
    source_url: str
    year: int | None = None
    category: str | None = None
    genres: list[str]
    count: int
    coverage: Literal["complete", "partial"]
    updated_at: datetime
    covers: list[str]
    pinned: bool = False
    tracking: bool = False
    saved: bool = False
    warning: str | None = None


class CollectionIndex(BaseModel):
    items: list[CollectionCard]
    total: int
    years: list[int]
    genres: list[str]
    categories: list[str]
    archive_gaps: list[int]
    providers: list[str] = Field(default_factory=list)
    audiences: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)


class CollectionEntry(CollectionBook):
    work: WorkView | None = None


class CollectionDetail(BaseModel):
    stale: bool = False
    collection: CollectionCard
    items: list[CollectionEntry]
    total: int
    page: int
    has_more: bool


class CollectionURL(BaseModel):
    url: str = Field(min_length=1, max_length=2000)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        source(value)
        return value


class CollectionFollowInput(BaseModel):
    pinned: bool = True
    tracking: bool = True


class AddCollection(CollectionURL, CollectionFollowInput):
    pass


def card(value, followed=None):
    from app.adapters.catalog_types import cover_url

    return CollectionCard(
        **{k: v for k, v in value.items() if k != "books"},
        covers=[
            url
            for b in value["books"]
            if (url := image_url(b.get("cover_url")) or cover_url(b.get("cover_url")))
        ][:4],
        pinned=followed.pinned if followed else False,
        tracking=followed.tracking if followed else False,
        saved=followed is not None,
        warning=followed.error
        if followed
        and datetime.fromisoformat(followed.snapshot["updated_at"])
        >= datetime.fromisoformat(value["updated_at"])
        else None,
    )


async def sources(db, user):
    follows = {
        r.collection_id: r
        for r in await db.scalars(select(DiscoveryFollow).where(DiscoveryFollow.user_id == user.id))
    }
    live = live_catalog()
    values = {**catalog(), **live}
    for row in await db.scalars(
        select(ProviderCache).where(ProviderCache.key.in_([snapshot_key(key) for key in live]))
    ):
        if row.value.get("id") in live:
            values[row.value["id"]] = row.value
    for key, row in follows.items():
        current = values.get(key)
        # A saved layout must not hide a newer app manifest or public chart snapshot.
        if not current or (
            current.get("refresh_mode") != "app-update"
            and (
                not current["books"]
                or datetime.fromisoformat(row.snapshot["updated_at"])
                > datetime.fromisoformat(current["updated_at"])
            )
        ):
            values[key] = row.snapshot
    return values, follows


async def collection_works(db, user, books):
    works = {}
    for provider in {b.provider for b in books}:
        works.update(
            await displayed_provider_works(
                db, user, provider, [b for b in books if b.provider == provider]
            )
        )
    return works


async def load_live(value, user, db):
    if value.get("provider", "goodreads") not in {"nyt", "audible"}:
        return user, value, None
    cached = await db.get(ProviderCache, snapshot_key(value["id"]))
    if cached and cached.expires_at > datetime.now(UTC):
        latest = max((value, cached.value), key=lambda v: datetime.fromisoformat(v["updated_at"]))
        return user, latest, None
    uid = user.id
    if value["books"]:
        from app.domain.catalog_refresh import schedule

        await db.rollback()
        if await schedule(
            uid, "local", None, "collection_snapshot", [value["id"], value["updated_at"]]
        ):
            return (
                await current_actor(db, uid),
                value,
                "Showing saved books while this collection refreshes.",
            )
    await db.rollback()
    from app.domain.curation_sources import fetch

    try:
        current = await fetch(value)
        await save_snapshot(current)
        warning = None
    except (AdapterError, ValueError):
        if not value["books"]:
            raise HTTPException(
                503, "This collection is temporarily unavailable. Try again later."
            ) from None
        current, warning = value, "Source unavailable; showing the last successful snapshot."
    return await current_actor(db, uid), current, warning


@router.get("/layout", response_model=DiscoveryPreferences)
async def layout(user: CurrentUser, db: Database):
    row = await db.get(DiscoveryLayout, user.id)
    return DiscoveryPreferences.model_validate(row.preferences if row else {})


@router.put("/layout", response_model=DiscoveryPreferences)
async def save_layout(body: DiscoveryPreferences, user: CurrentUser, db: Database):
    await transaction_lock(db, f"discovery-layout:{user.id}")
    row = await db.get(DiscoveryLayout, user.id)
    if not row:
        row = DiscoveryLayout(user_id=user.id)
        db.add(row)
    kept = (row.preferences or {}).get("release_genres", []) if row else []
    row.preferences = {**body.model_dump(), "release_genres": kept}
    await db.commit()
    return body


@router.get("/collections", response_model=CollectionIndex)
async def collections(
    user: CurrentUser,
    db: Database,
    kind: Literal["all", "award", "listopia", "chart", "collections"] = "all",
    provider: str = "",
    audience: str = "",
    language: str = "",
    q: str = Query(default="", max_length=150),
    genre: str = "",
    category: str = "",
    year: int | None = None,
    saved: bool = False,
    ids: Annotated[list[str] | None, Query(max_length=200)] = None,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=24, ge=1, le=100),
):
    values, follows = await sources(db, user)
    return collection_index(
        values,
        follows,
        kind=kind,
        provider=provider,
        audience=audience,
        language=language,
        q=q,
        genre=genre,
        category=category,
        year=year,
        saved=saved,
        ids=ids,
        page=page,
        limit=limit,
    )


def collection_index(
    values,
    follows,
    *,
    kind="all",
    provider="",
    audience="",
    language="",
    q="",
    genre="",
    category="",
    year=None,
    saved=False,
    ids=None,
    page=1,
    limit=24,
):
    rows = [
        v
        for v in values.values()
        if kind == "all" or v["kind"] == kind or (kind == "collections" and v["kind"] != "award")
    ]
    years = sorted({v["year"] for v in rows if v.get("year")}, reverse=True)
    genres = sorted({g for v in rows for g in v["genres"]})
    providers = sorted({v.get("provider", "goodreads") for v in rows})
    audiences = sorted({v.get("audience", "all") for v in rows})
    languages = sorted({v["language"] for v in rows if v.get("language")})
    categories = sorted({v["category"] for v in rows if v.get("category")})
    gaps = [
        int(y)
        for y, v in coverage().get("years", {}).items()
        if v["expected"] is None or v["expected"] != v["available"]
    ]
    rows = [
        v
        for v in rows
        if (kind == "all" or v["kind"] == kind or (kind == "collections" and v["kind"] != "award"))
        and (not provider or v.get("provider", "goodreads") == provider)
        and (not audience or v.get("audience", "all") == audience)
        and (not language or v.get("language") == language)
        and (ids is None or v["id"] in ids)
        and (not saved or v["id"] in follows)
        and (not year or v.get("year") == year)
        and (not genre or genre in v["genres"])
        and (not category or category == v.get("category"))
        and (not q or q.casefold() in v["title"].casefold())
    ]
    rows.sort(key=lambda v: (-(v.get("year") or 0), v["title"], v["id"]))
    return CollectionIndex(
        items=[card(v, follows.get(v["id"])) for v in rows[(page - 1) * limit : page * limit]],
        total=len(rows),
        years=years,
        genres=genres,
        categories=categories,
        archive_gaps=sorted(gaps),
        providers=providers,
        audiences=audiences,
        languages=languages,
    )


class DiscoveryHome(BaseModel):
    index: CollectionIndex
    public_lists: CollectionIndex
    saved: CollectionIndex
    layout: DiscoveryPreferences
    selected: list[CollectionCard]


@router.get("/home", response_model=DiscoveryHome)
async def home(user: CurrentUser, db: Database):
    # Load reader snapshots once for the initial shelves, not once per filter.
    values, follows = await sources(db, user)
    preferences = await layout(user, db)
    return DiscoveryHome(
        index=collection_index(values, follows, limit=100),
        public_lists=collection_index(values, follows, kind="listopia", limit=100),
        saved=collection_index(values, follows, saved=True, limit=100),
        layout=preferences,
        selected=[
            card(values[key], follows.get(key)) for key in preferences.order if key in values
        ],
    )


async def detail_view(value, user, db, followed=None, page=1, winners=False, q="", full=False):
    books = [
        CollectionBook.model_validate(b)
        for b in value["books"]
        if (not winners or b.get("winner"))
        and (not q or q.casefold() in (b["title"] + " " + " ".join(b["authors"])).casefold())
    ]
    stale = False
    total = len(books)
    books = books[(page - 1) * 40 : page * 40]
    if full and value["kind"] == "listopia":
        if winners or q:
            raise HTTPException(422, "Full list browsing does not support snapshot filters")
        user_id = user.id
        try:
            raw, total, stale = await collection_page(db, user_id, value, page)
        except AdapterError as error:
            await db.rollback()
            raise HTTPException(
                502,
                "Goodreads could not load more books. "
                "Your loaded books are still available. Try again.",
            ) from error
        user = await current_actor(db, user_id)
        current, follows = await sources(db, user)
        latest = current.get(value["id"])
        if not latest:
            raise HTTPException(404, "Collection no longer available")
        if latest["updated_at"] != value["updated_at"]:
            raise HTTPException(409, "Collection changed while loading; refresh it")
        followed = follows.get(value["id"])
        books = [CollectionBook.model_validate(b) for b in raw]
    works = await collection_works(db, user, books)
    availability = await availability_for(db, user, list({w.id for w in works.values()}))
    items = []
    for book in books:
        work = works.get((book.provider, book.external_id))
        items.append(
            CollectionEntry(
                **book.model_dump(), work=work_view(work, availability[work.id]) if work else None
            )
        )
    return CollectionDetail(
        collection=card(value, followed),
        stale=stale,
        items=items,
        total=total,
        page=page,
        has_more=page * 40 < total,
    )


@router.post("/collections/preview", response_model=CollectionDetail)
async def preview(body: CollectionURL, user: Member, db: Database):
    _, key, url = source(body.url)
    if "/best-books-" in url:
        raise HTTPException(422, "Choose a category in Awards to add it to Discover.")
    values, follows = await sources(db, user)
    value = values.get(key)
    if not value:
        user_id = user.id
        await db.rollback()
        try:
            value = await fetch_collection(url)
        except AdapterError as error:
            raise HTTPException(
                502, "Goodreads could not load this list. Try again later."
            ) from error
        user = await current_actor(db, user_id, edit=True)
    return await detail_view(value, user, db, follows.get(key))


@router.post("/collections", response_model=CollectionCard)
async def add_collection(body: AddCollection, user: Member, db: Database):
    _, key, url = source(body.url)
    if "/best-books-" in url:
        raise HTTPException(422, "Choose a category in Awards first.")
    values, _ = await sources(db, user)
    value = values.get(key)
    user_id = user.id
    if not value:
        await db.rollback()
        try:
            value = await fetch_collection(url)
        except AdapterError as error:
            raise HTTPException(
                502, "Goodreads could not load this list. Try again later."
            ) from error
    return await save_follow(value, body, user_id, db)


async def save_follow(value, body, user_id, db):
    key = value["id"]
    await transaction_lock(db, f"discovery-follow:{user_id}:{key}")
    await current_actor(db, user_id, edit=True)
    row = await db.get(DiscoveryFollow, (user_id, key))
    if not row:
        row = DiscoveryFollow(user_id=user_id, collection_id=key, snapshot=value, generation=0)
        db.add(row)
    if datetime.fromisoformat(value["updated_at"]) > datetime.fromisoformat(
        row.snapshot["updated_at"]
    ):
        row.error = None
    row.snapshot = value
    row.pinned, row.tracking = body.pinned, body.tracking
    row.generation += 1
    row.next_check_at = datetime.now(UTC) + timedelta(hours=24)
    if body.pinned:
        await transaction_lock(db, f"discovery-layout:{user_id}")
        preferences = await db.get(DiscoveryLayout, user_id)
        if preferences and key in preferences.preferences.get("hidden", []):
            preferences.preferences = {
                **preferences.preferences,
                "hidden": [v for v in preferences.preferences["hidden"] if v != key],
            }
    await db.commit()
    return card(value, row)


@router.get("/collections/{collection_id}", response_model=CollectionDetail)
async def collection(
    collection_id: str,
    user: CurrentUser,
    db: Database,
    page: int = Query(default=1, ge=1),
    winners: bool = False,
    full: bool = False,
    q: str = Query(default="", max_length=150),
):
    values, follows = await sources(db, user)
    if collection_id not in values:
        raise HTTPException(404, "Collection not found")
    user, value, warning = await load_live(values[collection_id], user, db)
    _, follows = await sources(db, user)
    result = await detail_view(value, user, db, follows.get(collection_id), page, winners, q, full)
    if warning:
        result.stale = True
        result.collection.warning = warning
    return result


@router.put("/collections/{collection_id}/follow", response_model=CollectionCard)
async def follow(collection_id: str, body: CollectionFollowInput, user: Member, db: Database):
    values, _ = await sources(db, user)
    if collection_id not in values:
        raise HTTPException(404, "Collection not found")
    user, value, _ = await load_live(values[collection_id], user, db)
    return await save_follow(value, body, user.id, db)


class DiscoveryBookPage(BaseModel):
    items: list[CollectionEntry]
    total: int
    page: int
    has_more: bool


@router.get("/browse", response_model=DiscoveryBookPage)
async def browse_books(
    user: CurrentUser,
    db: Database,
    provider: str = "",
    audience: str = "",
    language: str = "",
    q: str = Query(default="", max_length=150),
    genre: str = "",
    category: str = "",
    year: int | None = None,
    winners: bool = False,
    page: int = Query(default=1, ge=1),
    owned: bool = False,
):
    values, _ = await sources(db, user)
    selected = sorted(values.values(), key=lambda v: (-(v.get("year") or 0), v["id"]))
    books = {}
    for v in selected:
        if (
            (provider and v.get("provider", "goodreads") != provider)
            or (audience and v.get("audience", "all") != audience)
            or (language and v.get("language") != language)
            or (genre and genre not in v["genres"])
            or (year and year != v.get("year"))
            or (category and category != v.get("category"))
        ):
            continue
        for b in v["books"]:
            if winners and not b.get("winner"):
                continue
            if q and q.casefold() not in (b["title"] + " " + " ".join(b["authors"])).casefold():
                continue
            books.setdefault((b.get("provider", "goodreads"), b["external_id"]), b)
    # Filtering ownership requires projection before paging, not just on the visible sample.
    all_books = [CollectionBook.model_validate(b) for b in books.values()]
    works = {}
    if owned:
        for start in range(0, len(all_books), 200):
            works.update(await collection_works(db, user, all_books[start : start + 200]))
        states = await availability_for(db, user, list({w.id for w in works.values()}))
        all_books = [
            b
            for b in all_books
            if (w := works.get((b.provider, b.external_id))) and states[w.id].owned
        ]
    total = len(all_books)
    batch = all_books[(page - 1) * 40 : page * 40]
    if not owned:
        works = await collection_works(db, user, batch)
        states = await availability_for(db, user, list({w.id for w in works.values()}))
    return DiscoveryBookPage(
        items=[
            CollectionEntry(
                **b.model_dump(),
                work=work_view(w, states[w.id])
                if (w := works.get((b.provider, b.external_id)))
                else None,
            )
            for b in batch
        ],
        total=total,
        page=page,
        has_more=page * 40 < total,
    )


@router.post("/collections/{collection_id}/refresh", response_model=CollectionCard)
async def refresh_collection(collection_id: str, user: Member, db: Database):
    from app.jobs.queue import enqueue

    row = await db.get(DiscoveryFollow, (user.id, collection_id), with_for_update=True)
    if not row or not row.tracking:
        raise HTTPException(409, "Enable tracking to refresh this collection")
    if row.next_check_at > datetime.now(UTC) + timedelta(hours=23, minutes=55):
        raise HTTPException(
            429, "This collection was checked recently. Try again in a few minutes."
        )
    row.generation += 1
    row.next_check_at = datetime.now(UTC) + timedelta(hours=24)
    await enqueue(
        db,
        "discovery.refresh",
        user_id=str(user.id),
        collection_id=collection_id,
        generation=row.generation,
    )
    await db.commit()
    return card(row.snapshot, row)


class PersonalListURL(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    tracking: bool = True


class PersonalListPreview(BaseModel):
    name: str
    count: int
    titles: list[str]
    list_id: str | None = None
    partial: bool = False


async def storygraph_personal(body, user, db):
    from app.adapters import storygraph
    from app.db.models import StorygraphAccount
    from app.db.session import session_factory
    from app.domain.storygraph_subscriptions import (
        BUSY,
        LIMITED,
        fetch_lock,
        save_rotation,
        storygraph_budget,
    )
    from app.security import decrypt_secrets

    try:
        target = storygraph.list_url(body.url)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    account = await db.get(StorygraphAccount, user.id)
    if not account:
        raise HTTPException(
            409, "Connect StoryGraph in Reading accounts before following this list"
        )
    uid = user.id
    await db.rollback()
    async with fetch_lock(uid, wait=False) as acquired:
        if not acquired:
            raise HTTPException(429, BUSY)
        async with session_factory()() as gate, gate.begin():
            wait = await storygraph_budget(gate, uid)
        if wait:
            raise HTTPException(429, LIMITED)
        account = await db.get(StorygraphAccount, uid)
        if not account:
            raise HTTPException(
                409, "Connect StoryGraph in Reading accounts before following this list"
            )
        saved = decrypt_secrets(account.encrypted_config)
        sent_cookie = saved.get("session_cookie") if isinstance(saved, dict) else None
        await db.rollback()
        live = {}
        try:
            page = await storygraph.read_list(
                storygraph.open_session(saved), target, session_out=live
            )
        except AdapterError as error:
            await save_rotation(db, uid, sent_cookie, live.get("session_cookie"))
            await db.commit()
            raise adapter_http_error(error) from error
        user = await current_actor(db, uid, edit=True)
        account = await db.get(StorygraphAccount, user.id, populate_existing=True)
        if not account:
            raise HTTPException(
                409, "Connect StoryGraph in Reading accounts before following this list"
            )
        await save_rotation(db, uid, saved["session_cookie"], page.session_cookie)
        current = decrypt_secrets(account.encrypted_config)
        username = target.username or current["username"]
        config = {"kind": target.kind, "id": target.id, "name": page.name, "username": username}
        await db.commit()
    return user, config, page, current["username"]


async def personal_preview(body, user, db):
    from urllib.parse import urlsplit

    from app.adapters import storygraph
    from app.adapters.goodreads import fetch_feed
    from app.adapters.goodreads_profile import profile_input, shelf_url

    if storygraph.is_storygraph_host(urlsplit(body.url.strip()).hostname):
        return await storygraph_personal(body, user, db)
    try:
        config = profile_input(body.url)
        url = shelf_url(config, config["selected"] or "to-read")
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    uid = user.id
    await db.rollback()
    try:
        value = await fetch_feed(url)
    except AdapterError as error:
        raise HTTPException(
            502, "This Goodreads shelf is unavailable. Check its visibility and URL."
        ) from error
    user = await current_actor(db, uid, edit=True)
    return user, config, url, value


@router.post("/personal-list/preview", response_model=PersonalListPreview)
async def preview_personal(body: PersonalListURL, user: Member, db: Database):
    from urllib.parse import urlsplit

    from app.adapters import storygraph
    from app.adapters.goodreads_profile import shelf_name

    if storygraph.is_storygraph_host(urlsplit(body.url.strip()).hostname):
        _, config, page, _seen = await personal_preview(body, user, db)
        await db.commit()
        return PersonalListPreview(
            name=config["name"],
            count=len(page.items),
            titles=[book["title"] for book in page.items[:4]],
            partial=page.partial,
        )
    _, config, _, value = await personal_preview(body, user, db)
    return PersonalListPreview(
        name=shelf_name(config["selected"] or "to-read"),
        count=len(value.items or []),
        titles=[b["title"] for b in (value.items or [])[:4]],
    )


@router.post("/personal-list", response_model=PersonalListPreview)
async def add_personal(body: PersonalListURL, user: Member, db: Database):
    from urllib.parse import urlsplit
    from uuid import uuid4

    from app.adapters import storygraph
    from app.adapters.goodreads import feed_identity
    from app.adapters.goodreads_profile import shelf_name
    from app.db.models import BookList, ListSubscription, StorygraphAccount
    from app.domain.list_subscriptions import begin
    from app.security import decrypt_secrets, encrypt_secrets

    if storygraph.is_storygraph_host(urlsplit(body.url.strip()).hostname):
        user, config, page, seen_username = await personal_preview(body, user, db)
        await transaction_lock(db, f"storygraph-account:{user.id}")
        account = await db.get(StorygraphAccount, user.id)
        latest = decrypt_secrets(account.encrypted_config).get("username") if account else None
        config = storygraph.retarget_config(config, seen_username, latest)
        ident = storygraph.identity(config)
        await transaction_lock(db, f"storygraph-follow:{user.id}:{ident}")
        rows = (
            await db.execute(
                select(BookList, ListSubscription)
                .join(ListSubscription)
                .where(BookList.owner_id == user.id, ListSubscription.provider == "storygraph")
            )
        ).all()
        for item, sub in rows:
            if storygraph.identity(decrypt_secrets(sub.encrypted_config)) == ident:
                await db.commit()
                return PersonalListPreview(
                    name=item.name, count=len(page.items), titles=[], list_id=str(item.id)
                )
        item = BookList(owner_id=user.id, name=config["name"][:200], shared=False)
        db.add(item)
        await db.flush()
        sub = ListSubscription(
            list_id=item.id,
            provider="storygraph",
            encrypted_config=encrypt_secrets(config),
            interval_minutes=60,
            enabled=body.tracking,
            next_sync_at=datetime.now(UTC),
        )
        db.add(sub)
        await db.flush()
        if body.tracking:
            await begin(db, user, item.id, f"discovery-follow:{uuid4()}")
        await db.commit()
        return PersonalListPreview(
            name=config["name"], count=len(page.items), titles=[], list_id=str(item.id)
        )
    user, config, url, value = await personal_preview(body, user, db)
    name = shelf_name(config["selected"] or "to-read")
    identity = feed_identity(url)
    await transaction_lock(
        db, f"goodreads-follow:{user.id}:{config['user_id']}:{config['selected'] or 'to-read'}"
    )
    rows = (
        await db.execute(
            select(BookList, ListSubscription)
            .join(ListSubscription)
            .where(BookList.owner_id == user.id, ListSubscription.provider == "goodreads")
        )
    ).all()
    for item, sub in rows:
        if feed_identity(decrypt_secrets(sub.encrypted_config)["url"]) == identity:
            return PersonalListPreview(
                name=item.name, count=len(value.items or []), titles=[], list_id=str(item.id)
            )
    item = BookList(owner_id=user.id, name=name, shared=False)
    db.add(item)
    await db.flush()
    sub = ListSubscription(
        list_id=item.id,
        provider="goodreads",
        encrypted_config=encrypt_secrets({"url": url}),
        interval_minutes=60,
        enabled=body.tracking,
        next_sync_at=datetime.now(UTC),
    )
    db.add(sub)
    await db.flush()
    if body.tracking:
        await begin(db, user, item.id, f"discovery-follow:{uuid4()}")
    await db.commit()
    return PersonalListPreview(
        name=name, count=len(value.items or []), titles=[], list_id=str(item.id)
    )


class GoodreadsBookResolution(BaseModel):
    entry: CollectionEntry
    match: MatchResult


@router.get("/curation/{external_id}", response_model=GoodreadsBookResolution)
@router.get("/goodreads/{external_id}", response_model=GoodreadsBookResolution)
async def resolve_goodreads_book(external_id: str, user: CurrentUser, db: Database):
    from app.api.metadata import adapter_http_error, provider_call
    from app.db.models import CatalogAccount
    from app.domain.discovery_matching import resolve_entry

    values, _ = await sources(db, user)
    raw = await find_book(db, user.id, values, external_id)
    if raw is None:
        raise HTTPException(404, "This book is no longer in your discovery collections")
    entry = CollectionEntry.model_validate(raw)
    if entry.subject != "work":
        # Awards for a performance or illustrated edition must not resolve straight to
        # a generic work request. The reader must choose the intended edition.
        return GoodreadsBookResolution(
            entry=entry,
            match=MatchResult(
                status="needs-review",
                reason="This selection recognizes a specific edition or performance. "
                "Choose its edition before requesting.",
            ),
        )
    user_id = user.id
    account = await db.get(CatalogAccount, user_id)
    if not account or not account.enabled:
        return GoodreadsBookResolution(
            entry=entry,
            match=MatchResult(
                status="disabled", reason="Connect Hardcover to load full book details."
            ),
        )

    async def call(operation, *args):
        return await provider_call(db, user_id, "hardcover", operation, *args)

    try:
        match = await resolve_entry(entry, call)
    except AdapterError as error:
        raise adapter_http_error(error) from error
    user = await current_actor(db, user_id)
    # A private/manual collection may have changed while provider calls were in flight.
    current, _ = await sources(db, user)
    if await find_book(db, user.id, current, external_id) != raw:
        raise HTTPException(409, "The source list changed. Reload the collection.")
    if match.book:
        bindings = await displayed_provider_works(db, user, "hardcover", [match.book])
        work = bindings.get(("hardcover", match.book.external_id))
        if work:
            states = await availability_for(db, user, [work.id])
            entry.work = work_view(work, states[work.id])
    return GoodreadsBookResolution(entry=entry, match=match)
