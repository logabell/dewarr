import { usePagedQuery } from "../hooks/usePagedQuery";
import { browseCache, refreshPending } from "../queryPolicies";
import InfiniteScroll from "../components/InfiniteScroll";
import BookLink from "../components/BookLink";
import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ExternalLink, UserRound } from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import { BookCard, Loading, Notice } from "../components";
import BookCover from "../components/BookCover";
import DetailTabs from "../components/DetailTabs";

import FollowCatalog from "../components/FollowCatalog";

const tabs = [
  ["books", "Books"],
  ["about", "About the author"],
] as const;
export default function AuthorDetail({ canEdit }: { canEdit: boolean }) {
  const { externalId = "" } = useParams();
  return (
    <AuthorContent key={externalId} externalId={externalId} canEdit={canEdit} />
  );
}
function AuthorContent({
  externalId,
  canEdit,
}: {
  externalId: string;
  canEdit: boolean;
}) {
  const [params] = useSearchParams();
  const tab = params.get("tab") === "about" ? "about" : "books";
  const [imageFailed, setImageFailed] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const query = usePagedQuery({
    queryKey: ["author", externalId],
    ...browseCache,
    refetchInterval: refreshPending,
    queryFn: async (page, signal) => {
      const data = result(
        await api.GET("/api/metadata/authors/hardcover/{external_id}", {
          params: { path: { external_id: externalId }, query: { page } },
          signal,
        }),
      );
      return { ...data, items: data.books };
    },
    next: (last, pages) =>
      last.has_more && pages.length < 100 ? pages.length + 1 : undefined,
    staleTime: 300_000,
    retry: false,
  });
  const author = query.data?.author;
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
  }, [author?.external_id]);
  if (query.isPending) return <Loading />;
  if (!query.data || !author)
    return (
      <section className="reader-page">
        <Link to="/" className="back-link">
          <ArrowLeft size={16} /> Back to catalog
        </Link>
        <h1>Author details unavailable</h1>
        <Notice error={query.error} />
        <button onClick={() => query.refetch()} disabled={query.isFetching}>
          Try again
        </button>
        <Link to="/metadata">Catalog connection settings</Link>
      </section>
    );
  const data = query.data;
  const knownWorks = Object.assign(
    {},
    ...(query.loadedPages || []).map((p) => p.known_works || {}),
  );
  return (
    <article className="reader-page catalog-reader entity-detail">
      <div className="book-topbar">
        <Link to="/" className="back-link">
          <ArrowLeft size={16} /> Back to catalog
        </Link>
      </div>
      <header className="entity-hero">
        <div className="author-portrait">
          {author.image_url && !imageFailed ? (
            <img
              src={author.image_url}
              alt={author.name}
              referrerPolicy="no-referrer"
              onError={() => setImageFailed(true)}
            />
          ) : (
            <UserRound size={72} aria-hidden="true" />
          )}
        </div>
        <div className="entity-hero-copy">
          <p className="eyebrow">THE AUTHOR</p>
          <h1 ref={heading} tabIndex={-1}>
            {author.name}
          </h1>
          <p className="entity-intro">
            Explore the books and the person behind them.
          </p>
          {author.bio && <p className="entity-bio-preview">{author.bio}</p>}
          {author.bio && (
            <Link className="reader-action-link" to="?tab=about">
              Read biography →
            </Link>
          )}
          {canEdit && (
            <FollowCatalog
              kind="author"
              externalId={externalId}
              name={author.name}
            />
          )}
          <div className="reader-outbound">
            <a
              href={`https://hardcover.app/authors/${encodeURIComponent(author.slug || externalId)}`}
              target="_blank"
              rel="noreferrer"
            >
              View on Hardcover <ExternalLink size={13} />
            </a>
          </div>
        </div>
      </header>
      {data.warning && (
        <p className="notice" role="status">
          {data.warning}
        </p>
      )}
      <DetailTabs tabs={tabs} selected={tab} label="Author sections" />
      <div
        id="detail-tab-panel"
        role="tabpanel"
        aria-labelledby={`detail-tab-${tab}`}
        tabIndex={0}
      >
        {tab === "about" ? (
          <section className="reader-section">
            <p className="eyebrow">BEHIND THE BOOKS</p>
            <h2>About {author.name}</h2>
            <p className="reader-prose reader-synopsis">
              {author.bio || "No biography is available for this author yet."}
            </p>
          </section>
        ) : (
          <section className="book-tab-section">
            <div className="book-tab-heading">
              <h2>Books by {author.name}</h2>
              <span className="muted">Publication order</span>
            </div>
            <p className="muted">
              Explore a title for editions, reviews, and library availability.
            </p>
            <div className="entity-book-grid">
              {data.items.map((book) => {
                const work = knownWorks[book.external_id];
                return work ? (
                  <BookCard
                    key={book.external_id}
                    work={work}
                    cover={book.cover_url}
                  />
                ) : (
                  <BookLink
                    key={book.external_id}
                    className="book-card discovery-book"
                    to={`/discover/books/hardcover/${book.external_id}`}
                    aria-label={`View ${book.title}`}
                  >
                    <BookCover
                      title={book.title}
                      cover={book.cover_url}
                      providerBook={{
                        provider: "hardcover",
                        external_id: book.external_id,
                      }}
                    />
                    <h3>{book.title}</h3>
                    <p>{book.publication_year || "Publication date unknown"}</p>
                  </BookLink>
                );
              })}
            </div>
            {!data.items.length && (
              <p className="notice">
                No books are available on this page. Check back as the catalog
                grows.
              </p>
            )}
            <InfiniteScroll query={query} />
          </section>
        )}
      </div>
    </article>
  );
}
