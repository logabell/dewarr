import { useState } from "react";
import { browseCache, refreshPending } from "../queryPolicies";
import BookSourceIcon from "./BookSourceIcon";
import { useQuery } from "@tanstack/react-query";
import { Star } from "lucide-react";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";

type Details = components["schemas"]["ReaderDetails"];
export function useReaderDetails(externalId?: string) {
  return useQuery({
    queryKey: ["reader-details", externalId],
    ...browseCache,
    refetchInterval: refreshPending,
    enabled: !!externalId,
    queryFn: async () =>
      result(
        await api.GET(
          "/api/metadata/books/hardcover/{external_id}/reader-details",
          {
            params: { path: { external_id: externalId! } },
          },
        ),
      ),
    retry: false,
    staleTime: 300_000,
  });
}

export function HardcoverRating({ details }: { details?: Details }) {
  if (!details || details.rating == null || !details.ratings_count) return null;
  return (
    <span className="reader-rating">
      <Star size={20} aria-hidden="true" />
      <strong>{details.rating.toFixed(2)}</strong>
      <span>
        / 5 · {details.ratings_count.toLocaleString()} ratings on Hardcover
      </span>
    </span>
  );
}

export default function BookReaderDetails({
  externalId,
  section = "all",
}: {
  externalId: string;
  section?: "all" | "authors" | "reviews";
}) {
  const query = useReaderDetails(externalId);
  const data = query.data;
  const [sort, setSort] = useState("helpful");
  const reviews = [...(data?.reviews || [])];
  if (sort === "recent")
    reviews.sort((a, b) =>
      (b.reviewed_at || "").localeCompare(a.reviewed_at || ""),
    );
  if (sort === "rating")
    reviews.sort((a, b) => (b.rating ?? -1) - (a.rating ?? -1));
  const url = `https://hardcover.app/books/${encodeURIComponent(data?.slug || externalId)}`;
  return (
    <div className="reader-community">
      <Notice error={query.error} />
      {query.isPending && <Loading />}
      {query.error && (
        <button onClick={() => query.refetch()} disabled={query.isFetching}>
          Retry author details and reviews
        </button>
      )}
      {data?.warning && <p className="notice">{data.warning}</p>}
      {section !== "reviews" && !!data?.authors?.length && (
        <section
          id="authors"
          className="reader-section"
          aria-labelledby="authors-heading"
        >
          <div className="book-tab-heading">
            <h2 id="authors-heading">Authors</h2>
          </div>
          <div className="book-table-scroll">
            <table className="book-data-table book-author-table">
              <thead>
                <tr>
                  <th scope="col">Author</th>
                  <th scope="col">Biography</th>
                </tr>
              </thead>
              <tbody>
                {data.authors.map((author) => (
                  <tr key={author.external_id}>
                    <td>
                      <Link to={`/authors/hardcover/${author.external_id}`}>
                        {author.name}
                      </Link>
                    </td>
                    <td>
                      <p className={author.bio ? "reader-prose" : "muted"}>
                        {author.bio || "No biography available."}
                      </p>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {section !== "authors" && (
        <section
          id="reviews"
          className="reader-section"
          aria-labelledby="reviews-heading"
        >
          <div className="reviews-toolbar">
            <div>
              <h2 id="reviews-heading">Reader reviews</h2>
              <p className="muted">From the Hardcover community</p>
            </div>
            <a
              className="reviews-source"
              href={url}
              target="_blank"
              rel="noreferrer"
            >
              View on Hardcover <BookSourceIcon source="Hardcover" />
            </a>
          </div>
          <div className="reviews-summary">
            <HardcoverRating details={data} />
            {!!reviews.length && (
              <label>
                Sort reviews
                <select
                  value={sort}
                  onChange={(event) => setSort(event.target.value)}
                >
                  <option value="helpful">Most liked</option>
                  <option value="recent">Newest first</option>
                  <option value="rating">Highest rated</option>
                </select>
              </label>
            )}
          </div>
          {data?.reviews_warning ? (
            <p className="notice">{data.reviews_warning}</p>
          ) : data && !(data.reviews || []).length ? (
            <p className="muted">
              No public written reviews are available for this book yet.
            </p>
          ) : null}
          {!!reviews.length && (
            <>
              <p className="reviews-count muted">
                {reviews.length} public{" "}
                {reviews.length === 1 ? "review" : "reviews"} · Sorting applies
                to the reviews shown
              </p>
              <div className="reader-reviews">
                {reviews.map((review) => (
                  <ReaderReview key={review.external_id} review={review} />
                ))}
              </div>
            </>
          )}
        </section>
      )}
      {section === "authors" && data && !data.authors?.length && (
        <p className="muted">
          No author biography is available from Hardcover yet.
        </p>
      )}
    </div>
  );
}

function ReaderReview({
  review,
}: {
  review: components["schemas"]["BookReview"];
}) {
  const [expanded, setExpanded] = useState(false);
  const long = review.text.length > 400;
  return (
    <article className="reader-review">
      <header>
        <span className="review-avatar" aria-hidden="true">
          {review.username.slice(0, 1).toUpperCase()}
        </span>
        <div className="review-byline">
          <a
            href={`https://hardcover.app/@${encodeURIComponent(review.username)}`}
            target="_blank"
            rel="noreferrer"
          >
            @{review.username}
          </a>
          {review.reviewed_at && (
            <time dateTime={review.reviewed_at}>
              {new Date(review.reviewed_at).toLocaleDateString(undefined, {
                month: "short",
                day: "numeric",
                year: "numeric",
                timeZone: "UTC",
              })}
            </time>
          )}
        </div>
        {review.rating != null && (
          <span className="reader-rating review-score">
            <Star size={14} aria-hidden="true" /> {review.rating}
            <span>/ 5</span>
          </span>
        )}
      </header>
      {review.spoilers ? (
        <details className="review-spoiler">
          <summary>Contains spoilers · Reveal review</summary>
          <p className="reader-prose">{review.text}</p>
        </details>
      ) : (
        <>
          <p className="reader-prose">
            {long && !expanded ? `${review.text.slice(0, 400)}…` : review.text}
          </p>
          {long && (
            <button
              className="review-expand"
              aria-expanded={expanded}
              onClick={() => setExpanded(!expanded)}
            >
              {expanded ? "Show less" : "Read full review"}
            </button>
          )}
        </>
      )}
    </article>
  );
}

export function useLocalBookMetadata(workId: string) {
  return useQuery({
    queryKey: ["reader-work-metadata", workId, 0],
    refetchInterval: (query) =>
      ["queued", "running", "retrying"].includes(
        query.state.data?.enrichment?.status || "",
      )
        ? 3000
        : false,
    queryFn: async () =>
      result(
        await api.GET("/api/metadata/works/{work_id}", {
          params: {
            path: { work_id: workId },
            query: { offset: 0, limit: 20, scope: "display" },
          },
        }),
      ),
  });
}

export function LocalBookReaderDetails({ workId }: { workId: string }) {
  const metadata = useLocalBookMetadata(workId);
  const source = metadata.data?.sources.find(
    (source) => source.provider === "hardcover",
  );
  return source ? <BookReaderDetails externalId={source.external_id} /> : null;
}
