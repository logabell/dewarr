import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "../components/InfiniteScroll";
import BookLink from "../components/BookLink";
import ListChoice from "./ListChoice";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
import { ArrowLeft, ExternalLink, LibraryBig } from "lucide-react";
import BookCover from "../components/BookCover";
import DetailTabs from "../components/DetailTabs";
import SeriesRequests from "./SeriesRequests";
import SeriesScopeReview from "./SeriesScopeReview";
import FollowCatalog from "../components/FollowCatalog";
import FollowRelease from "../components/FollowRelease";
import { randomUUID } from "../randomUUID";

function missingWorkIds(
  items: {
    compilation: boolean;
    partial: boolean;
    merged_record: boolean;
    ambiguous_position: boolean;
    publication: string;
    work: {
      id: string;
      availability: { owned: boolean; ebook: boolean; audio: boolean };
    };
  }[],
  medium: "any" | "ebook" | "audio",
) {
  return [
    ...new Set(
      items
        .filter(
          (entry) =>
            !entry.compilation &&
            !entry.partial &&
            !entry.merged_record &&
            !entry.ambiguous_position &&
            entry.publication === "published" &&
            (medium === "any"
              ? !entry.work.availability.owned
              : !entry.work.availability[medium]),
        )
        .map((entry) => entry.work.id),
    ),
  ];
}

export default function Series({ canEdit }: { canEdit: boolean }) {
  const { externalId = "" } = useParams();
  return (
    <SeriesContent key={externalId} externalId={externalId} canEdit={canEdit} />
  );
}

function SeriesContent({
  externalId,
  canEdit,
}: {
  externalId: string;
  canEdit: boolean;
}) {
  const cache = useQueryClient();
  const [params, setParams] = useSearchParams();
  const tabs = canEdit
    ? ([
        ["books", "Reading order"],
        ["about", "About the series"],
        ["requests", "Lists & requests"],
      ] as const)
    : ([
        ["books", "Reading order"],
        ["about", "About the series"],
      ] as const);
  const tab = tabs.some(([key]) => key === params.get("tab"))
    ? params.get("tab")!
    : canEdit && params.has("request")
      ? "requests"
      : "books";
  const [listId, setListId] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [added, setAdded] = useState(0);
  const queryKey = ["series", externalId];
  const catalog = usePagedQuery({
    queryKey,
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/catalog/series/hardcover/{external_id}", {
          signal,
          params: {
            path: { external_id: externalId },
            query: { offset, limit: 50 },
          },
        }),
      ),
    refetchInterval: (query) =>
      ["queued", "running", "retrying"].includes(
        query.state.data?.pages[0]?.status || "",
      )
        ? 1500
        : false,
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.total ? count : undefined;
    },
  });
  const mainBooks = useQuery({
    queryKey: ["series-main-books", externalId, catalog.data?.generation],
    queryFn: async () =>
      result(
        await api.GET(
          "/api/catalog/series/hardcover/{external_id}/main-books",
          { params: { path: { external_id: externalId } } },
        ),
      ),
    enabled: canEdit && Boolean(catalog.data?.fetched_at),
  });
  const policy = useQuery({
    queryKey: ["list-policy", listId],
    queryFn: async () =>
      result(
        await api.GET("/api/lists/{list_id}/acquisition", {
          params: { path: { list_id: listId } },
        }),
      ),
    enabled: canEdit && Boolean(listId),
  });
  const refresh = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/catalog/series/hardcover/{external_id}/refresh", {
          params: {
            path: { external_id: externalId },
            header: { "idempotency-key": randomUUID() },
          },
        }),
      ),
    onSuccess: async () => {
      setSelected([]);
      await cache.invalidateQueries({ queryKey: ["series", externalId] });
    },
  });
  const add = useMutation({
    mutationFn: async () => {
      // Each existing list command is idempotent. Retain failed selections so a
      // partial network failure can be retried without repeating successful work.
      const ids = [...selected];
      const results: PromiseSettledResult<unknown>[] = [];
      for (let offset = 0; offset < ids.length; offset += 5) {
        results.push(
          ...(await Promise.allSettled(
            ids.slice(offset, offset + 5).map(async (work_id) =>
              result(
                await api.POST("/api/lists/{list_id}/entries", {
                  params: { path: { list_id: listId } },
                  body: { work_id },
                }),
              ),
            ),
          )),
        );
      }
      const failed = ids.filter(
        (_, index) => results[index].status === "rejected",
      );
      setSelected(failed);
      setAdded(ids.length - failed.length);
      await cache.invalidateQueries({ queryKey: ["lists"] });
      if (failed.length)
        throw new Error(
          `${failed.length} books could not be added. Successful additions were kept; retry the remaining selection.`,
        );
    },
  });
  const gapsRequested = params.get("gaps") === "1";
  const gapMedium =
    params.get("medium") === "ebook" || params.get("medium") === "audio"
      ? (params.get("medium") as "ebook" | "audio")
      : "any";
  const gapsApplied = useRef(false);
  useEffect(() => {
    void (async () => {
      try {
        result(
          await api.POST("/api/discovery/series/seen", {
            body: { external_id: externalId },
          }),
        );
      } catch {
        // The series page still works if the new marker cannot be cleared.
      }
    })();
  }, [externalId]);
  useEffect(() => {
    if (
      !gapsRequested ||
      !catalog.hasNextPage ||
      catalog.isFetchingNextPage ||
      catalog.isFetchNextPageError
    )
      return;
    void catalog.fetchNextPage();
  }, [
    gapsRequested,
    catalog.hasNextPage,
    catalog.isFetchingNextPage,
    catalog.isFetchNextPageError,
    catalog.fetchNextPage,
  ]);
  useEffect(() => {
    if (
      !gapsRequested ||
      gapsApplied.current ||
      !catalog.data ||
      catalog.hasNextPage ||
      catalog.isFetchingNextPage
    )
      return;
    gapsApplied.current = true;
    setSelected(missingWorkIds(catalog.data.items, gapMedium));
  }, [
    gapsRequested,
    gapMedium,
    catalog.data,
    catalog.hasNextPage,
    catalog.isFetchingNextPage,
  ]);
  if (catalog.isPending) return <Loading />;
  if (!catalog.data) return <Notice error={catalog.error} />;
  const data = catalog.data;
  const loading = ["queued", "running", "retrying"].includes(data.status);
  const published = data.items
    .filter(
      (e) =>
        !e.compilation &&
        !e.partial &&
        !e.merged_record &&
        !e.ambiguous_position &&
        e.publication === "published",
    )
    .map((e) => e.work.id);
  return (
    <article className="reader-page catalog-reader entity-detail">
      <div className="book-topbar">
        <Link to="/" className="back-link">
          <ArrowLeft size={16} /> Back to catalog
        </Link>
      </div>
      <header className="entity-hero">
        <div className="series-cover-stack" aria-label="Books in this series">
          {data.items.length ? (
            data.items
              .slice(0, 3)
              .reverse()
              .map((entry) => (
                <div key={entry.membership_id}>
                  <BookCover title={entry.work.title} work={entry.work} />
                </div>
              ))
          ) : (
            <div className="series-cover-placeholder">
              <LibraryBig size={64} aria-hidden="true" />
            </div>
          )}
        </div>
        <div className="entity-hero-copy">
          <p className="eyebrow">THE SERIES</p>
          <h1>{data.name}</h1>
          {canEdit && (
            <FollowCatalog
              kind="series"
              externalId={externalId}
              name={data.name}
            />
          )}
          <p className="entity-intro">Find your place in the story.</p>
          {data.fetched_at && (
            <>
              <dl className="reader-facts">
                <div>
                  <dt>Books</dt>
                  <dd>{data.books}</dd>
                </div>
                <div>
                  <dt>In your library</dt>
                  <dd>{data.owned}</dd>
                </div>
                <div>
                  <dt>Ebooks</dt>
                  <dd>{data.ebook}</dd>
                </div>
                <div>
                  <dt>Audiobooks</dt>
                  <dd>{data.audio}</dd>
                </div>
              </dl>
              {data.books > 0 && (
                <div className="series-ownership">
                  <div
                    className="series-ownership-track"
                    role="meter"
                    aria-label="Series books in your library"
                    aria-valuemin={0}
                    aria-valuemax={data.books}
                    aria-valuenow={Math.min(data.owned, data.books)}
                  >
                    <span
                      style={{
                        width: `${Math.min(100, (data.owned / data.books) * 100)}%`,
                      }}
                    />
                  </div>
                  <span>
                    {data.owned} of {data.books} books in your library
                  </span>
                </div>
              )}
            </>
          )}
          <div className="reader-outbound">
            <a
              href={`https://hardcover.app/series/${encodeURIComponent(externalId)}`}
              target="_blank"
              rel="noreferrer"
            >
              View on Hardcover <ExternalLink size={13} />
            </a>
          </div>
        </div>
      </header>
      {data.message && (
        <p className="muted" role="status">
          {data.message}
        </p>
      )}
      <Notice
        error={
          catalog.error ||
          refresh.error ||
          add.error ||
          policy.error ||
          mainBooks.error
        }
      />
      {canEdit && (
        <div className="button-row">
          <button
            disabled={refresh.isPending || loading || add.isPending}
            onClick={() => refresh.mutate()}
          >
            {data.fetched_at ? "Refresh series" : "Load series from Hardcover"}
          </button>
          {data.fetched_at && data.books > data.owned && (
            <button
              type="button"
              onClick={() => {
                setParams({ tab: "requests", gaps: "1" });
                if (catalog.data && !catalog.hasNextPage) {
                  gapsApplied.current = true;
                  setSelected(missingWorkIds(catalog.data.items, "any"));
                } else gapsApplied.current = false;
              }}
            >
              Request missing books
            </button>
          )}
        </div>
      )}
      {gapsRequested && (catalog.hasNextPage || catalog.isFetchingNextPage) && (
        <p className="muted" role="status">
          Loading the rest of this series to select missing books.
        </p>
      )}
      <DetailTabs tabs={tabs} selected={tab} label="Series sections" />
      <div
        id="detail-tab-panel"
        role="tabpanel"
        aria-labelledby={`detail-tab-${tab}`}
        tabIndex={0}
      >
        {tab === "about" && (
          <section className="reader-section">
            <p className="eyebrow">THE BIGGER STORY</p>
            <h2>About {data.name}</h2>
            <p className="reader-prose reader-synopsis">
              {data.description ||
                "No description is available for this series yet."}
            </p>
            <details className="editor">
              <summary>About this series catalog</summary>
              {data.fetched_at && (
                <p className="muted">
                  Last verified {new Date(data.fetched_at).toLocaleString()} ·{" "}
                  {data.total} entries.
                </p>
              )}
              <p className="muted">
                Book counts exclude compilations, partial books and merged
                records. Uncertain entries remain visible for review. Available
                downloads may contain a different selection of books.
              </p>
            </details>
          </section>
        )}
        {tab !== "about" && (
          <section className="book-tab-section">
            <div className="book-tab-heading">
              <h2>
                {tab === "requests" ? "Build your collection" : "Reading order"}
              </h2>
              <span className="muted">{data.total} catalog entries</span>
            </div>
            <p className="muted">
              {tab === "requests"
                ? "Choose books to add to a reading list or request missing formats."
                : "Follow the series sequence. Compilations and uncertain positions are marked below."}
            </p>
            {tab === "requests" && canEdit && data.items.length > 0 && (
              <section className="panel editor" aria-label="Curate series">
                <ListChoice
                  value={listId}
                  label="Destination list"
                  disabled={add.isPending}
                  onChange={(id) => {
                    setListId(id);
                    setAdded(0);
                  }}
                />
                {listId && policy.isSuccess && (
                  <p role="status">
                    {policy.data?.active &&
                    policy.data.configuration.mode === "automatic"
                      ? "This list automatically requests missing media for new additions."
                      : "This list has no active automatic acquisition policy."}
                  </p>
                )}
                <button
                  disabled={
                    add.isPending ||
                    loading ||
                    new Set([...selected, ...published]).size > 100
                  }
                  onClick={() =>
                    setSelected((previous) => [
                      ...new Set([...previous, ...published]),
                    ])
                  }
                >
                  Select published books on this page
                </button>
                <button
                  disabled={
                    !listId ||
                    !selected.length ||
                    add.isPending ||
                    loading ||
                    !policy.isSuccess
                  }
                  onClick={() => add.mutate()}
                >
                  Add selected books to list ({selected.length})
                </button>
                {added > 0 && (
                  <p role="status">Added {added} books to the list.</p>
                )}
              </section>
            )}
            <div className="series-book-list">
              {data.items.map((entry) => (
                <article className="series-book-row" key={entry.membership_id}>
                  <div className="series-position">
                    <span>{entry.position ?? "—"}</span>
                    <small>{entry.compilation ? "collection" : "book"}</small>
                  </div>
                  <BookLink
                    aria-label={`View ${entry.work.title}`}
                    className="series-row-cover"
                    to={`/books/${entry.work.id}`}
                  >
                    <BookCover title={entry.work.title} work={entry.work} />
                  </BookLink>
                  <div className="series-row-copy">
                    {tab === "requests" && canEdit && (
                      <label className="check-label">
                        <input
                          type="checkbox"
                          checked={selected.includes(entry.work.id)}
                          disabled={
                            add.isPending ||
                            loading ||
                            entry.publication === "unreleased" ||
                            (!selected.includes(entry.work.id) &&
                              selected.length >= 100)
                          }
                          onChange={(e) =>
                            setSelected((previous) =>
                              e.target.checked
                                ? [...new Set([...previous, entry.work.id])]
                                : previous.filter((id) => id !== entry.work.id),
                            )
                          }
                        />
                        Select {entry.work.title}
                      </label>
                    )}
                    <h2>
                      <Link to={`/books/${entry.work.id}`}>
                        {entry.work.title}
                      </Link>
                    </h2>
                    <p>{entry.work.authors.join(", ") || "Author unknown"}</p>
                    <p>
                      {entry.work.availability.owned
                        ? "✓ In library"
                        : "Not in your library"}
                      {entry.work.availability.ebook && " · Ebook"}
                      {entry.work.availability.audio && " · Audiobook"}
                      {entry.work.availability.stale &&
                        " · Inventory needs refresh"}
                    </p>
                    <p className="muted">
                      {[
                        entry.compilation && "Compilation",
                        entry.partial && "Partial book",
                        entry.merged_record && "Merged provider record",
                        entry.ambiguous_position &&
                          "Multiple works at this position",
                        entry.publication === "unreleased" &&
                          `Unreleased · ${entry.release_date || "date unknown"}`,
                        entry.publication === "unknown" &&
                          "Publication date unknown",
                        entry.details !== entry.position && entry.details,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                    {entry.publication === "unreleased" && (
                      <FollowRelease
                        canEdit={canEdit}
                        following={entry.followed}
                        workId={entry.work.id}
                        body={{
                          work_id: entry.work.id,
                          title: entry.work.title,
                          authors: entry.work.authors,
                          release_date: entry.release_date,
                          basis: "work",
                        }}
                      />
                    )}
                  </div>
                </article>
              ))}
            </div>
            {data.fetched_at && !data.total && (
              <p>No accessible books were returned for this series.</p>
            )}
            <InfiniteScroll query={catalog} />
            {tab === "requests" && canEdit && data.fetched_at && (
              <>
                {mainBooks.data && !loading && (
                  <SeriesScopeReview
                    externalId={externalId}
                    generation={data.generation}
                    selected={selected}
                    review={mainBooks.data}
                    onSelect={setSelected}
                  />
                )}
                <SeriesRequests
                  externalId={externalId}
                  generation={data.generation}
                  selected={selected}
                  mainBookReview={mainBooks.data}
                />
              </>
            )}
          </section>
        )}
      </div>
    </article>
  );
}
