"""Bounded, read-only public collection extraction. No login or challenge bypass."""

import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, field_validator

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.goodreads import MAX_BYTES, fetch_document

BASE = "https://www.goodreads.com"


class CollectionBook(BaseModel):
    provider: str = "goodreads"
    source_url: str | None = None
    status: Literal["winner", "finalist", "honor", "longlist", "listed"] = "listed"
    subject: Literal["work", "recording", "edition"] = "work"
    language: str | None = None
    narrators: list[str] = Field(default_factory=list)
    contributors: list[str] = Field(default_factory=list)
    rating: float | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    external_id: str
    title: str
    authors: list[str] = Field(default_factory=list)
    cover_url: str | None = None
    identifiers: dict[str, str] = Field(default_factory=dict)
    rank: int | None = None
    votes: int | None = None
    winner: bool = False

    @field_validator("cover_url", mode="before")
    @classmethod
    def full_size_cover(cls, value):
        from app.adapters.catalog_types import cover_url

        return image_url(value) or cover_url(value)


def source(value):
    """Canonical public URLs only; reject credentials, queries and concatenated URLs."""
    try:
        p = urlsplit(value.strip())
        if (
            p.scheme != "https"
            or p.hostname not in {"goodreads.com", "www.goodreads.com"}
            or p.username
            or p.password
            or p.port not in {None, 443}
            or p.query
            or p.fragment
            or "\\" in value
            or len(value) > 2000
        ):
            raise ValueError
        if m := re.fullmatch(r"/list/show/([1-9][0-9]{0,15})(?:\.[\w-]+)?/?", p.path):
            return "listopia", f"gr-list-{m[1]}", f"{BASE}/list/show/{m[1]}"
        if m := re.fullmatch(r"/choiceawards/([a-z][a-z-]{1,150})-(20[0-9]{2})/?", p.path):
            return "award", f"gr-award-{m[1]}-{m[2]}", BASE + p.path.rstrip("/")
    except ValueError:
        pass
    raise ValueError("Paste one Goodreads Listopia or Choice Awards URL")


def soup_for(content):
    if not content or len(content) > MAX_BYTES:
        raise ValueError("Collection response is empty or too large")
    soup = BeautifulSoup(content, "html.parser")
    if not soup.select('a[href*="/book/show/"]'):
        raise ValueError("Goodreads did not return book entries. Try again later.")
    return soup


def image_url(value):
    if not isinstance(value, str):
        return None
    p = urlsplit(value)
    if (
        p.scheme == "https"
        and p.hostname in {"i.gr-assets.com", "images.gr-assets.com"}
        and not p.username
        and not p.password
        and p.port in {None, 443}
    ):
        # Listopia serves 50–75px thumbnails. Request a real 600px source image.
        return re.sub(r"\._(?:SX|SY)\d+_\.", "._SY600_.", value)
    return None


def book_id(link):
    match = re.match(r"/book/show/([0-9]+)", urlsplit(link.get("href", "")).path)
    if not match:
        raise ValueError("A book identity is missing")
    return match[1]


def number(text):
    m = re.search(r"([\d,]+)", text)
    return int(m[1].replace(",", "")) if m else None


def average_rating(row):
    node = row.select_one('[itemprop="ratingValue"]')
    text = (node.get("content") or node.get_text(strip=True)) if node else ""
    if not text:
        node = row.select_one(".minirating")
        match = (
            re.search(r"([0-5](?:\.\d+)?)\s+avg rating", node.get_text(" ", strip=True))
            if node
            else None
        )
        text = match[1] if match else ""
    try:
        value = float(text)
        return value if 0 < value <= 5 else None
    except ValueError:
        return None


def list_pagination(soup, key):
    """Exclude the separate comment paginator that appears below Listopia books."""
    identity = re.escape(key.removeprefix("gr-list-"))
    return next(
        (
            node
            for node in soup.select(".pagination")
            if any(
                re.fullmatch(
                    rf"/list/show/{identity}(?:\.[\w-]+)?/?", urlsplit(a.get("href", "")).path
                )
                for a in node.select("a[href]")
            )
        ),
        None,
    )


def parse_collection(content, url, title=None):
    kind, key, canonical = source(url)
    soup = soup_for(content)
    books = []
    year = int(canonical[-4:]) if kind == "award" else None
    if kind == "award":
        winner = soup.select_one("a.winningTitle")
        winner_id = book_id(winner) if winner else None
        for position, link in enumerate(soup.select(".pollAnswer__bookLink"), 1):
            img = link.find("img")
            caption = (img.get("title") or img.get("alt") or "") if img else ""
            name, separator, author = caption.rpartition(" by ")
            if not separator:
                name, author = caption, ""
            parent = link.find_parent(class_="pollAnswer")
            vote_node = parent.select_one(".result") if parent else None
            books.append(
                CollectionBook(
                    external_id=book_id(link),
                    title=name,
                    authors=[author] if author else [],
                    cover_url=image_url(img.get("src")) if img else None,
                    rank=position,
                    votes=number(vote_node.get_text()) if vote_node else None,
                    winner=book_id(link) == winner_id,
                )
            )
        if not winner_id or not any(b.winner for b in books):
            raise ValueError("Final award results are not available yet")
        heading = soup.title.get_text().split("—")[0].strip() if soup.title else "Choice Awards"
        count, coverage = len(books), "complete"
        if len(books) < 10 or len(books) > 30:
            raise ValueError("Award nominee coverage needs review")
    else:
        for row in soup.select('tr[itemtype$="/Book"]'):
            link = row.select_one("a.bookTitle")
            if not link:
                continue
            img = row.select_one("img.bookCover")
            rank = row.select_one(".number")
            isbn = row.select_one('[itemprop="isbn"]')
            books.append(
                CollectionBook(
                    external_id=book_id(link),
                    title=link.get_text(" ", strip=True),
                    rating=average_rating(row),
                    authors=[a.get_text(" ", strip=True) for a in row.select("a.authorName")],
                    cover_url=image_url(img.get("src")) if img else None,
                    identifiers={"isbn": isbn.get("content") or isbn.get_text(strip=True)}
                    if isbn
                    else {},
                    rank=number(rank.get_text()) if rank else None,
                )
            )
        h = soup.select_one("h1.gr-h1") or soup.select_one("h1")
        heading = (
            re.sub(r"\s*\([\d,]+ books\).*", "", soup.title.get_text()).strip()
            if soup.title
            else h.get_text(" ", strip=True)
            if h
            else "Goodreads list"
        )
        count_match = re.search(r"([\d,]+) books", soup.get_text(" ", strip=True))
        count = number(count_match[1]) if count_match else len(books)
        pager = list_pagination(soup, key)
        coverage = (
            "partial"
            if count > len(books) or (pager and pager.select_one("a.next_page"))
            else "complete"
        )
    if not books or len(books) > 100 or any(not b.title for b in books):
        raise ValueError("Goodreads returned an incomplete collection")
    if len({b.external_id for b in books}) != len(books):
        raise ValueError("Goodreads returned duplicate book identities")
    return {
        "id": key,
        "kind": kind,
        "title": title or heading,
        "source_url": canonical,
        "year": year,
        "category": title if kind == "award" else None,
        "genres": [],
        "count": count,
        "coverage": coverage,
        "updated_at": datetime.now(UTC).isoformat(),
        "books": [b.model_dump() for b in books],
    }


def award_categories(content, year):
    soup = BeautifulSoup(content, "html.parser")
    categories = {}
    for link in soup.select('a[href*="/choiceawards/"]'):
        href = link.get("href", "")
        label = link.get_text(" ", strip=True)
        if (
            href.endswith(f"-{year}")
            and not href.endswith(f"best-books-{year}")
            and label
            and label != "View results"
        ):
            categories[BASE + href if href.startswith("/") else href] = label
    return categories


def category_genres(category):
    """Keep source category labels while mapping them onto the browse taxonomy."""
    label = category.lower()
    genres = set()
    for term, genre in {
        "fantasy": "fantasy",
        "romantasy": "fantasy",
        "science fiction": "science-fiction",
        "sci-fi": "science-fiction",
        "romance": "romance",
        "mystery": "mystery",
        "horror": "horror",
        "historical fiction": "historical-fiction",
        "nonfiction": "non-fiction",
        "history": "history",
        "biography": "biography",
        "memoir": "biography",
        "young adult": "young-adult",
        "children": "children",
        "picture books": "children",
        "middle grade": "children",
    }.items():
        if term in label:
            genres.add(genre)
    if "romantasy" in label:
        genres.add("romance")
    return sorted(genres)


async def fetch_collection(url):
    kind, key, canonical = source(url)
    try:
        document = await fetch_document(
            canonical,
            html=True,
            collection_id=key.removeprefix("gr-list-") if kind == "listopia" else None,
        )
        return parse_collection(document.content, canonical)
    except ValueError as error:
        raise AdapterError(FailureKind.PARSER, str(error)) from error


async def fetch_collection_page(url, page):
    """Fetch one ranked Listopia page, rejecting silently repeated or truncated pages."""
    kind, key, canonical = source(url)
    if kind != "listopia" or not isinstance(page, int) or page < 1:
        raise ValueError("Use a positive page number for a Goodreads list")
    try:
        document = await fetch_document(
            f"{canonical}?page={page}",
            html=True,
            collection_id=key.removeprefix("gr-list-"),
            collection_page=page,
        )
        value = parse_collection(document.content, canonical)
        books = value["books"]
        start = (page - 1) * 100
        soup = soup_for(document.content)
        pager = list_pagination(soup, key)
        current = pager.select_one(".current") if pager else None
        ranks = [b["rank"] for b in books]
        # Tied votes can repeat a rank (including across page boundaries).
        # Verify the source's active page instead of requiring consecutive ranks.
        if (
            (page > 1 and current is None)
            or (current is not None and number(current.get_text()) != page)
            or any(rank is None or rank < 1 or rank > start + 100 for rank in ranks)
            or ranks != sorted(ranks)
        ):
            raise ValueError("Goodreads did not return the requested list page")
        has_more = bool(pager and pager.select_one("a.next_page[href]"))
        if pager is None and value["count"] > start + len(books):
            raise ValueError("Goodreads did not return list pagination")
        if has_more and len(books) != 100:
            raise ValueError("Goodreads returned a truncated list page")
        return {
            "books": books,
            "count": max(value["count"], start + len(books) + 1)
            if has_more
            else start + len(books),
            "has_more": has_more,
        }
    except ValueError as error:
        raise AdapterError(FailureKind.PARSER, str(error)) from error
