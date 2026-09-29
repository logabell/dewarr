import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "../components/InfiniteScroll";
import BookLink from "../components/BookLink";
import ListChoice from "./ListChoice";
import { useEffect, useRef, useState, type SetStateAction } from "react";
import type { components } from "../api/schema";
import type { Choice } from "./RequestPreferences";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
import {
  ArrowLeft,
  ExternalLink,
  LibraryBig,
  BookOpen,
  Headphones,
} from "lucide-react";
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
    metadata_incomplete?: boolean;
    category?: string;
    work: {
      id: string;
      availability: { owned: boolean; ebook: boolean; audio: boolean };
    };
  }[],
  medium: "either" | "both" | "ebook" | "audio",
) {
  return [
    ...new Set(
      items
        .filter(
          (entry) =>
            !entry.metadata_incomplete &&
            entry.category !== "collection" &&
            !entry.partial &&
            !entry.merged_record &&
            !entry.ambiguous_position &&
            entry.publication === "published" &&
            (medium === "either"
              ? !entry.work.availability.ebook && !entry.work.availability.audio
              : medium === "both"
                ? !entry.work.availability.ebook ||
                  !entry.work.availability.audio
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
        ["about", "About"],
        ["requests", "Requests"],
        ["lists", "Lists"],
      ] as const)
    : ([
        ["books", "Reading order"],
        ["about", "About"],
      ] as const);
  const tab = tabs.some(([key]) => key === params.get("tab"))
    ? params.get("tab")!
    : canEdit && params.has("request")
      ? "requests"
      : "books";
  const [listId, setListId] = useState("");
  const [manualSelected, setManualSelected] = useState<string[]>([]);
  const [selectMissing, setSelectMissing] = useState(
    params.get("gaps") === "1",
  );
  const [spec, setSpec] = useState<components["schemas"]["RequestOptions"]>(
    () => ({
      mode:
        params.get("medium") === "ebook" || params.get("medium") === "audio"
          ? (params.get("medium") as "ebook" | "audio")
          : undefined,
    }),
  );
  const [preferences, setPreferences] = useState<Choice>({});
  const profiles = useQuery({
    queryKey: ["release-profiles"],
    enabled: canEdit,
    queryFn: async () => result(await api.GET("/api/acquisition/profiles")),
  });
  const profile = profiles.data?.find(
    (p) => (p.id || "") === (preferences.profile_id || ""),
  );
  const effectiveMode =
    spec.mode ||
    preferences.overrides?.desired_media ||
    profile?.preferences.desired_media;
  const [added, setAdded] = useState(0);
  const section =
    params.get("section") === "all"
      ? "all"
      : params.get("section") === "supplements"
        ? "supplements"
        : "main";
  const queryKey = ["series", externalId, section];
  const catalog = usePagedQuery({
    queryKey,
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/catalog/series/hardcover/{external_id}", {
          signal,
          params: {
            path: { external_id: externalId },
            query: { offset, limit: 50, section },
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
  const missingIds =
    effectiveMode && catalog.data
      ? missingWorkIds(catalog.data.items, effectiveMode)
      : [];
  const selected =
    selectMissing && tab === "requests"
      ? missingIds.slice(0, 100)
      : manualSelected;
  const setSelected = (value: SetStateAction<string[]>) => {
    setManualSelected(typeof value === "function" ? value(selected) : value);
    setSelectMissing(false);
  };
  const missingCounts = { ebook: 0, audio: 0, either: 0 };
  const counted = new Set<string>();
  for (const entry of catalog.data?.items || []) {
    if (!selected.includes(entry.work.id) || counted.has(entry.work.id))
      continue;
    counted.add(entry.work.id);
    const { ebook, audio } = entry.work.availability;
    if (effectiveMode === "either") {
      if (!ebook && !audio) missingCounts.either++;
    } else {
      if ((effectiveMode === "ebook" || effectiveMode === "both") && !ebook)
        missingCounts.ebook++;
      if ((effectiveMode === "audio" || effectiveMode === "both") && !audio)
        missingCounts.audio++;
    }
  }
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
  const autoLoaded = useRef(new Set<string>());
  useEffect(() => {
    if (
      canEdit &&
      (catalog.data?.status === "not-loaded" ||
        (catalog.data?.fetched_at &&
          (catalog.data?.projection_version || 0) < 2 &&
          !["queued", "running", "retrying"].includes(
            catalog.data?.status || "",
          ))) &&
      !autoLoaded.current.has(externalId)
    ) {
      autoLoaded.current.add(externalId);
      refresh.mutate();
    }
  }, [
    canEdit,
    externalId,
    catalog.data?.status,
    catalog.data?.fetched_at,
    catalog.data?.projection_version,
    refresh.mutate,
  ]);
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
  const gapsRequested = selectMissing && tab === "requests";
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
  if (catalog.isPending) return <Loading />;
  if (!catalog.data) return <Notice error={catalog.error} />;
  const data = catalog.data;
  const loading = ["queued", "running", "retrying"].includes(data.status);
  const published = data.items
    .filter(
      (e) =>
        (section === "main" || !e.compilation) &&
        !e.metadata_incomplete &&
        !e.partial &&
        !e.merged_record &&
        !e.ambiguous_position &&
        e.publication === "published",
    )
    .map((e) => e.work.id);
  return (
    <article className="reader-page catalog-reader entity-detail series-detail">
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
                  <BookCover
                    title={entry.work.title}
                    work={entry.work}
                    actions={tab !== "requests" && tab !== "lists"}
                  />
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
          <p className="entity-intro">
            {data.authors?.join(", ") || "Books in reading order"}
          </p>
          {data.fetched_at &&
            (tab === "requests" || tab === "lists" ? (
              <p className="series-compact-summary">
                {data.books} main books · {data.owned} in your library
              </p>
            ) : (
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
            ))}
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
      {data.message &&
        (loading || !data.fetched_at || data.status === "failed") && (
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
          {data.fetched_at && data.books > 0 && (
            <button
              type="button"
              onClick={() => {
                setParams({ tab: "requests", gaps: "1" });
                setSelectMissing(true);
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
            {data.description ? (
              <p className="reader-prose reader-synopsis">{data.description}</p>
            ) : (
              <p className="reader-prose">
                {data.authors?.length
                  ? `${data.name} is a series by ${data.authors.join(", ")}. `
                  : ""}
                {data.books} main books are listed in the English reading order
                {data.supplements
                  ? `, with ${data.supplements} supplementary works available separately`
                  : ""}
                .
              </p>
            )}
            <dl className="series-about-facts">
              <div>
                <dt>Author{(data.authors?.length || 0) > 1 ? "s" : ""}</dt>
                <dd>{data.authors?.join(", ") || "Not provided"}</dd>
              </div>
              <div>
                <dt>Catalog language</dt>
                <dd>English</dd>
              </div>
              <div>
                <dt>Reading order</dt>
                <dd>
                  {data.books} main · {data.supplements} supplementary
                </dd>
              </div>
            </dl>
            {!data.description && (
              <p className="muted">
                Hardcover has not provided a series synopsis.
              </p>
            )}
            {!!data.incomplete_entries && (
              <p className="muted">
                {data.incomplete_entries} incomplete catalog{" "}
                {data.incomplete_entries === 1 ? "record is" : "records are"}{" "}
                available under All catalog entries.
              </p>
            )}
            <details className="editor">
              <summary>About this series catalog</summary>
              {data.fetched_at && (
                <p className="muted">
                  Last verified {new Date(data.fetched_at).toLocaleString()} ·{" "}
                  {data.raw_total} catalog entries.
                </p>
              )}
              <p className="muted">
                Main books show one English catalog representative per
                whole-number position. Supplements, editions and collections
                remain available under All catalog entries. These suggestions do
                not merge records or authorize downloads.
              </p>
            </details>
          </section>
        )}
        {tab !== "about" && (
          <section className="book-tab-section">
            <div className="book-tab-heading">
              <h2>
                {tab === "requests"
                  ? "Request books"
                  : tab === "lists"
                    ? "Add books to a list"
                    : "Reading order"}
              </h2>
              <span className="muted">
                {data.books} main books
                {data.planned > 0 ? ` · ${data.planned} planned` : ""} ·{" "}
                {data.supplements} supplements
              </span>
            </div>
            <p className="muted">
              {tab === "requests"
                ? "Select books, choose a format, then review your request."
                : tab === "lists"
                  ? "Choose books for your reading list."
                  : "Explore the main books or switch to supplementary reading."}
            </p>
            <div className="series-selection-toolbar">
              <label className="field series-filter">
                Show
                <select
                  value={section}
                  onChange={(event) => {
                    const next = new URLSearchParams(params);
                    next.set("section", event.target.value);
                    setParams(next);
                    setSelected([]);
                  }}
                >
                  <option value="main">Main books · English</option>
                  <option value="supplements">
                    Supplementary reading · English
                  </option>
                  <option value="all">
                    All catalog entries ({data.raw_total})
                  </option>
                </select>
              </label>
              {(tab === "requests" || tab === "lists") && canEdit && (
                <div className="button-row series-selection-actions">
                  <button
                    disabled={
                      loading ||
                      add.isPending ||
                      (tab === "requests" && !effectiveMode)
                    }
                    onClick={() => {
                      if (tab === "requests") setSelectMissing(true);
                      else
                        setSelected(
                          missingWorkIds(data.items, "either").slice(0, 100),
                        );
                    }}
                  >
                    Select missing
                  </button>
                  <button
                    disabled={loading || add.isPending}
                    onClick={() =>
                      setSelected([...new Set(published)].slice(0, 100))
                    }
                  >
                    Select published
                  </button>
                  {selected.length > 0 && (
                    <button
                      disabled={add.isPending}
                      onClick={() => setSelected([])}
                    >
                      Clear ({selected.length})
                    </button>
                  )}
                </div>
              )}
            </div>
            <div
              className={
                tab === "requests" || tab === "lists" ? "series-workspace" : ""
              }
            >
              <div className="series-selection-books">
                <div className="series-book-list">
                  {data.items.map((entry) => (
                    <article
                      className="series-book-row"
                      key={entry.membership_id}
                    >
                      <div className="series-position">
                        <span>{entry.position ?? "—"}</span>
                        <small>
                          {entry.category === "collection"
                            ? "collection"
                            : "book"}
                        </small>
                      </div>
                      <BookLink
                        aria-label={`View ${entry.work.title}`}
                        className="series-row-cover"
                        to={`/books/${entry.work.id}`}
                      >
                        <BookCover
                          title={entry.work.title}
                          work={entry.work}
                          actions={tab !== "requests" && tab !== "lists"}
                        />
                      </BookLink>
                      <div className="series-row-copy">
                        {(tab === "requests" || tab === "lists") && canEdit && (
                          <label className="check-label">
                            <input
                              type="checkbox"
                              checked={selected.includes(entry.work.id)}
                              disabled={
                                add.isPending ||
                                loading ||
                                entry.publication === "unreleased" ||
                                entry.metadata_incomplete ||
                                (!selected.includes(entry.work.id) &&
                                  selected.length >= 100)
                              }
                              onChange={(e) =>
                                setSelected((previous) =>
                                  e.target.checked
                                    ? [...new Set([...previous, entry.work.id])]
                                    : previous.filter(
                                        (id) => id !== entry.work.id,
                                      ),
                                )
                              }
                            />
                            <span className="sr-only">
                              Select {entry.work.title}
                            </span>
                          </label>
                        )}
                        <h2>
                          <Link to={`/books/${entry.work.id}`}>
                            {entry.work.title}
                          </Link>
                        </h2>
                        <p>
                          {entry.work.authors.join(", ") || "Author unknown"}
                        </p>
                        <p className="series-format-status">
                          {entry.work.availability.owned
                            ? "✓ In library"
                            : "Not in your library"}
                          {entry.work.availability.ebook && (
                            <span>
                              <BookOpen size={14} aria-hidden="true" /> Ebook
                            </span>
                          )}
                          {entry.work.availability.audio && (
                            <span>
                              <Headphones size={14} aria-hidden="true" />{" "}
                              Audiobook
                            </span>
                          )}
                          {entry.work.availability.stale &&
                            " · Inventory needs refresh"}
                        </p>
                        <p className="muted">
                          {[
                            entry.metadata_incomplete &&
                              "Incomplete Hardcover record",
                            entry.category === "collection" && "Compilation",
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
              </div>
              <aside className="series-actions-column">
                {tab === "lists" && canEdit && data.items.length > 0 && (
                  <section
                    className="panel editor series-list-editor"
                    aria-label="Curate series"
                  >
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
                {tab === "requests" && canEdit && data.fetched_at && (
                  <SeriesRequests
                    externalId={externalId}
                    generation={data.generation}
                    selected={selected}
                    spec={spec}
                    onSpecChange={(next) => {
                      if (next.mode !== spec.mode) setSelectMissing(true);
                      setSpec(next);
                    }}
                    preferences={preferences}
                    onPreferencesChange={(next) => {
                      if (
                        next.profile_id !== preferences.profile_id ||
                        next.overrides?.desired_media !==
                          preferences.overrides?.desired_media
                      )
                        setSelectMissing(true);
                      setPreferences(next);
                    }}
                    effectiveMode={effectiveMode}
                    missingCounts={missingCounts}
                    selectionLoading={
                      gapsRequested &&
                      (catalog.hasNextPage || catalog.isFetchingNextPage)
                    }
                    selectionLimit={selectMissing && missingIds.length > 100}
                    defaultsError={profiles.error}
                    mainBookReview={mainBooks.data}
                    scopeReview={
                      mainBooks.data && !loading ? (
                        <SeriesScopeReview
                          externalId={externalId}
                          generation={data.generation}
                          selected={selected}
                          review={mainBooks.data}
                          onSelect={setSelected}
                        />
                      ) : undefined
                    }
                  />
                )}
              </aside>
            </div>
          </section>
        )}
      </div>
    </article>
  );
}
