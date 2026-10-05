"""Optional administrator-operated Audiobookshelf-compatible metadata endpoint."""

import hashlib
import json

from pydantic import ValidationError

from app.adapters.catalog_types import BookData, EditionData, SearchPage, cover_url, year
from app.adapters.contracts import AdapterError, FailureKind
from app.config import get_settings
from app.domain.catalog_language import catalog_language


def book_cache_key(external_id):
    import hmac

    from app.domain.corrections import revision

    settings = get_settings()
    token = (
        settings.custom_metadata_token.get_secret_value() if settings.custom_metadata_token else ""
    )
    digest = hmac.new(settings.encryption_key(), token.encode(), hashlib.sha256).hexdigest()
    return revision(["custom-book", settings.custom_metadata_url, digest, external_id])


class CustomMetadata:
    def __init__(self, request):
        self.request = request

    async def search(self, query, page=1, language=None):
        try:
            result = await self._search(query, page)
        except (ValidationError, TypeError, ValueError, AttributeError) as error:
            raise AdapterError(
                FailureKind.PARSER, "Custom metadata returned invalid book details"
            ) from error
        if language:
            result.items = [
                book for book in result.items if book.language == catalog_language(language)
            ]
        return result

    async def _search(self, query, page):
        if page != 1:
            return SearchPage(provider="custom", items=[], page=page, has_more=False)
        value = await self.request("GET", "search", params={"query": query})
        rows = value.get("matches")
        if not isinstance(rows, list) or len(rows) > 100:
            raise AdapterError(
                FailureKind.PARSER, "Custom metadata must return at most 100 matches"
            )
        books = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("title"), str):
                raise AdapterError(FailureKind.PARSER, "Custom metadata returned an invalid match")
            authors = [row["author"]] if isinstance(row.get("author"), str) else []
            identifiers = {k: str(row[k]) for k in ("isbn", "asin") if row.get(k)}
            # Stable within the endpoint. Detail fetches read only server-cached search results.
            key = hashlib.sha256(
                json.dumps(
                    [get_settings().custom_metadata_url, row["title"], authors, identifiers],
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            edition = EditionData(
                external_id=key,
                title=row["title"],
                identifiers=identifiers,
                language=catalog_language(row.get("language")),
                medium="audio" if row.get("narrator") or row.get("duration") else "unknown",
                narrators=[row["narrator"]] if isinstance(row.get("narrator"), str) else [],
                publication_year=year(row.get("publishedYear")),
                description=row.get("description"),
                publisher=row.get("publisher"),
                cover_url=cover_url(row.get("cover")),
            )
            books.append(
                BookData(
                    provider="custom",
                    external_id=key,
                    title=row["title"],
                    authors=authors,
                    language=edition.language,
                    description=edition.description,
                    cover_url=edition.cover_url,
                    publication_year=edition.publication_year,
                    editions=[edition],
                )
            )
        return SearchPage(provider="custom", items=books, page=page, has_more=False)

    async def fetch(self, external_id, offset=0):
        # ABS's search contract has no stable detail endpoint. Only server-cached search
        # results may be selected; arbitrary ids are never interpreted as paths or URLs.
        from app.db.models import ProviderCache
        from app.db.session import session_factory

        key = book_cache_key(external_id)
        async with session_factory()() as db:
            row = await db.get(ProviderCache, key)
            if row:
                from datetime import UTC, datetime

                if row.expires_at > datetime.now(UTC):
                    return BookData.model_validate(row.value)
        raise AdapterError(
            FailureKind.NOT_FOUND, "Search this custom source again before selecting a book"
        )


async def remember(page):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy.dialects.postgresql import insert

    from app.db.models import ProviderCache
    from app.db.session import session_factory

    async with session_factory()() as db, db.begin():
        for book in page.items:
            values = dict(
                key=book_cache_key(book.external_id),
                value=book.model_dump(mode="json"),
                fetched_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            await db.execute(
                insert(ProviderCache)
                .values(**values)
                .on_conflict_do_update(index_elements=[ProviderCache.key], set_=values)
            )
