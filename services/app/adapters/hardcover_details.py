"""Optional reader-facing details; public reviews never enter catalog metadata."""

from datetime import date, datetime

from pydantic import BaseModel, Field, ValidationError

from app.adapters.catalog_providers import identifier, parse_failure
from app.adapters.catalog_types import cover_url
from app.adapters.contracts import AdapterError, FailureKind

HC_DETAILS = """query ReaderBookDetails($id: Int!) {
 books(where: {id: {_eq: $id}}, limit: 1) {
  id slug pages audio_seconds release_date
  contributions(limit: 20) {
   contribution author { id name slug bio cached_image }
  }
 }
}"""
HC_REVIEWS = """query ReaderBookReviews($id: Int!) {
 books(where: {id: {_eq: $id}}, limit: 1) { id rating ratings_count }
 user_books(where: {book_id: {_eq: $id}, has_review: {_eq: true},
 privacy_setting_id: {_eq: 1}}, order_by: [{likes_count: desc}, {id: desc}], limit: 10) {
  id rating review_raw review_has_spoilers reviewed_at user { username }
 }
}"""


class AuthorDetails(BaseModel):
    external_id: str
    name: str
    slug: str | None = None
    bio: str | None = None
    image_url: str | None = None


class BookReview(BaseModel):
    external_id: str
    username: str
    rating: float | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    text: str
    spoilers: bool
    reviewed_at: datetime | None = None


class ReaderDetails(BaseModel):
    external_id: str
    slug: str | None = None
    rating: float | None = Field(default=None, ge=0, le=5, allow_inf_nan=False)
    ratings_count: int = Field(default=0, ge=0)
    pages: int | None = Field(default=None, ge=0)
    audio_seconds: int | None = Field(default=None, ge=0)
    release_date: date | None = None
    authors: list[AuthorDetails] = Field(default_factory=list)
    reviews: list[BookReview] = Field(default_factory=list)
    reviews_warning: str | None = None
    stale: bool = False
    warning: str | None = None


async def details(query, external_id):
    key = int(identifier("hardcover", external_id))
    try:
        rows = (await query(HC_DETAILS, {"id": key}))["books"]
        if rows == []:
            raise AdapterError(FailureKind.NOT_FOUND, "This Hardcover book is no longer available.")
        row = rows[0]
        if str(row["id"]) != external_id:
            raise parse_failure()
        authors = {}
        for contribution in row.get("contributions") or []:
            if contribution.get("contribution") not in {None, "Author"}:
                continue
            author = contribution["author"]
            author_id = identifier("hardcover", str(author["id"]))
            authors[author_id] = AuthorDetails(
                external_id=author_id,
                name=author["name"],
                slug=author.get("slug"),
                bio=author.get("bio"),
                image_url=cover_url((author.get("cached_image") or {}).get("url")),
            )
        result = ReaderDetails(
            external_id=external_id,
            slug=row.get("slug"),
            rating=row.get("rating"),
            ratings_count=row.get("ratings_count") or 0,
            pages=row.get("pages"),
            audio_seconds=row.get("audio_seconds"),
            release_date=row.get("release_date"),
            authors=list(authors.values()),
        )
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, ValidationError) as error:
        raise parse_failure() from error
    try:
        activity = await query(HC_REVIEWS, {"id": key})
        ratings = activity.get("books")
        if ratings is not None:
            if not isinstance(ratings, list) or len(ratings) != 1 or ratings[0].get("id") != key:
                raise parse_failure()
            # Community activity has a shorter TTL than descriptive metadata.
            current = ReaderDetails(
                external_id=external_id,
                rating=ratings[0].get("rating"),
                ratings_count=ratings[0].get("ratings_count") or 0,
            )
            result.rating, result.ratings_count = current.rating, current.ratings_count
        reviews = activity["user_books"]
        if not isinstance(reviews, list) or len(reviews) > 10:
            raise parse_failure()
        for review in reviews:
            text = review.get("review_raw")
            if not isinstance(text, str) or not text.strip():
                continue
            result.reviews.append(
                BookReview(
                    external_id=identifier("hardcover", str(review["id"])),
                    username=review["user"]["username"],
                    rating=review.get("rating"),
                    text=text,
                    spoilers=review.get("review_has_spoilers") is not False,
                    reviewed_at=review.get("reviewed_at"),
                )
            )
    except (
        AdapterError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        ValidationError,
    ) as error:
        if isinstance(error, AdapterError) and error.kind == FailureKind.AUTHENTICATION:
            # Credentials apply to the whole account, including cached book details.
            # Let the caller mark the account disconnected and suppress further requests.
            raise
        result.reviews_warning = (
            "Reviews are unavailable right now. You can read them on Hardcover."
        )
    return result
