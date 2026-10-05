import ShelfViewAll from "./ShelfViewAll";
import BookLink from "./BookLink";
import type { ReactNode } from "react";
import BookCover from "./BookCover";
import ShelfPagination from "./ShelfPagination";
import { Link } from "react-router-dom";
import type { components } from "../api/schema";
import { BookCard } from "../components";

export type Shelf = components["schemas"]["DiscoveryShelf"];
export type DiscoveryItem = components["schemas"]["DiscoveryItem"];

export default function DiscoveryShelf({
  shelf,
  controls,
  medium,
  grid = false,
  viewAll,
}: {
  shelf: Shelf;
  grid?: boolean;
  viewAll?: string;
  controls?: ReactNode;
  medium?: "any" | "ebook" | "audio";
}) {
  const items = shelf.items || [];
  return (
    <>
      <div className="section-heading discovery-heading">
        <div>
          <h2 title={shelf.attribution}>{shelf.title}</h2>
          <span className="sr-only">{shelf.attribution}</span>
        </div>
        {items.length > 0 &&
          (controls ||
            (items.length > 0 && (
              <ShelfPagination
                page={1}
                hasMore={false}
                busy={false}
                onPage={() => {}}
                label={shelf.title}
              />
            )))}
        {shelf.stale && <span className="count">Cached shelf</span>}
      </div>
      {shelf.warning && (
        <p className="notice" role="status">
          {shelf.warning}
        </p>
      )}
      {shelf.status === "not-connected" && (
        <Link className="back-link" to="/settings#catalog">
          Connect Hardcover
        </Link>
      )}
      {shelf.status === "ready" && items.length === 0 && (
        <p className="muted">No titles available for this shelf yet.</p>
      )}
      {items.length > 0 && (
        <ul
          className={grid ? "explore-books" : "discovery-shelf"}
          aria-label={shelf.title}
        >
          {items.map((item) => (
            <li
              key={
                item.work?.id ||
                `${item.book.provider}:${item.book.external_id}`
              }
            >
              {item.work ? (
                <BookCard
                  work={item.work}
                  rating={item.book.rating}
                  cover={item.book.cover_url}
                  medium={medium}
                />
              ) : (
                <BookLink
                  className="book-card discovery-book"
                  to={
                    item.book.provider && item.book.external_id
                      ? `/discover/books/${item.book.provider}/${encodeURIComponent(item.book.external_id)}`
                      : `/search?q=${encodeURIComponent(item.book.title)}`
                  }
                  aria-label={`View ${item.book.title}`}
                >
                  <BookCover
                    title={item.book.title}
                    providerBook={
                      item.book.external_id
                        ? {
                            provider: item.book.provider,
                            external_id: item.book.external_id,
                          }
                        : undefined
                    }
                    rating={item.book.rating}
                    cover={item.book.cover_url}
                    medium={medium}
                  />
                  <h3 title={item.book.title}>{item.book.title}</h3>
                  <p title={item.book.authors.join(", ")}>
                    {item.book.authors.join(", ") || "Author unknown"}
                  </p>
                </BookLink>
              )}
            </li>
          ))}
          {viewAll && !controls && <ShelfViewAll to={viewAll} />}
        </ul>
      )}
    </>
  );
}
