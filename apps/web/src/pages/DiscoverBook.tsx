import BookPagination from "../components/BookPagination";
import { browseCache, refreshPending } from "../queryPolicies";
import DetailTabs from "../components/DetailTabs";
import BookSourceIcon from "../components/BookSourceIcon";
import QuickAdd from "../components/QuickAdd";
import GoodreadsBook from "../components/GoodreadsDiscoveryBook";
import LibraryFormatBadges from "../components/LibraryFormatBadges";
import { languageName } from "../components/LanguageSelect";
import {
  BookHero,
  BookOverview,
  releaseIsAhead,
} from "../components/BookPresentation";
import FollowRelease, { useReleaseWatch } from "../components/FollowRelease";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, BookOpen, Check } from "lucide-react";
import {
  Link,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { api, result, type Auth, type Work } from "../api/client";
import { canStartDownload } from "../permissions";
import { Loading, Notice } from "../components";
import BookReaderDetails, {
  useReaderDetails,
} from "../components/BookReaderDetails";
import ListChoice from "./ListChoice";
import Wanted from "./Wanted";

type Provider = "hardcover" | "openlibrary";
type Action = "catalog" | "request" | "list" | "sources";
export default function DiscoverBook({ canEdit }: { canEdit: boolean }) {
  const { provider, externalId = "" } = useParams();
  if (provider === "goodreads") return <GoodreadsBook />;
  if (provider !== "hardcover" && provider !== "openlibrary")
    return (
      <p className="notice">
        Unknown book provider. <Link to="/discover">Return to Discover</Link>
      </p>
    );
  return (
    <BookPage
      key={`${provider}:${externalId}`}
      provider={provider}
      externalId={externalId}
      canEdit={canEdit}
    />
  );
}

function BookPage({
  provider,
  externalId,
  canEdit,
}: {
  provider: Provider;
  externalId: string;
  canEdit: boolean;
}) {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const tabs: [string, string][] = [
    ["overview", "Overview"],
    ["editions", "Editions"],
    ...(provider === "hardcover"
      ? ([
          ["authors", "Authors"],
          ["reviews", "Reviews"],
        ] as [string, string][])
      : []),
  ];
  const requestedTab = searchParams.get("tab") || "overview";
  const tab = tabs.some(([key]) => key === requestedTab)
    ? requestedTab
    : "overview";
  const location = useLocation();
  const searchReturn = location.state?.search;
  const fromSearch =
    typeof searchReturn === "string" && searchReturn.startsWith("/search?");
  const cache = useQueryClient();
  const session = useQuery<Auth | null>({
    queryKey: ["session"],
    enabled: false,
  });
  const canQuickAdd =
    canStartDownload(
      session.data?.user.permissions,
      session.data?.user.role,
      "ebook",
    ) ||
    canStartDownload(
      session.data?.user.permissions,
      session.data?.user.role,
      "audio",
    );
  const heading = useRef<HTMLHeadingElement>(null);
  const actionPanel = useRef<HTMLElement>(null);
  const [localWork, setLocalWork] = useState<Work | null>(null);
  const [action, setAction] = useState<Action | null>(null);
  const [listId, setListId] = useState("");
  const [format, setFormat] = useState("all");
  const [editionPage, setEditionPage] = useState(1);
  const preview = useQuery({
    queryKey: ["provider-book", provider, externalId],
    ...browseCache,
    staleTime: 60_000,
    refetchInterval: refreshPending,
    queryFn: async () =>
      result(
        await api.GET("/api/metadata/books/{provider}/{external_id}", {
          params: { path: { provider, external_id: externalId } },
        }),
      ),
    retry: false,
  });
  const community = useReaderDetails(
    provider === "hardcover" ? externalId : undefined,
  );
  const work = localWork || preview.data?.work;
  const providerBook = preview.data?.book;
  const book =
    providerBook && work
      ? {
          ...providerBook,
          title: work.title,
          authors: work.authors,
          description: work.description,
          cover_url: work.cover_url || providerBook.cover_url,
        }
      : providerBook;
  const unreleased = releaseIsAhead(
    community.data?.release_date,
    book?.publication_year,
  );
  const watch = useReleaseWatch(work?.id, unreleased);
  const watching =
    watch.data?.state === "waiting" || watch.data?.state === "wanted";
  useEffect(() => {
    if (book) {
      heading.current?.focus({ preventScroll: true });
      window.scrollTo(0, 0);
    }
  }, [book?.external_id]);
  useEffect(() => {
    if (action && work) actionPanel.current?.scrollIntoView({ block: "start" });
  }, [action, work?.id]);
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/metadata/books/{provider}/{external_id}/import", {
          params: { path: { provider, external_id: externalId } },
        }),
      ),
    onSuccess: (value) => {
      setLocalWork(value);
      for (const key of [
        "works",
        "discovery",
        "provider-search",
        "provider-book",
      ])
        cache.invalidateQueries({ queryKey: [key] });
      if (action === "sources") navigate(`/books/${value.id}?tab=sources`);
    },
  });
  function choose(next: Action) {
    setAction(next);
    if (work) {
      if (next === "sources") navigate(`/books/${work.id}?tab=sources`);
    } else save.mutate();
  }
  const addToList = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/lists/{list_id}/entries", {
          params: { path: { list_id: listId } },
          body: { work_id: work!.id },
        }),
      ),
    onSuccess: () => {
      cache.invalidateQueries({ queryKey: ["lists"] });
    },
  });
  const sourceName = provider === "hardcover" ? "Hardcover" : "Open Library";
  const externalUrl =
    provider === "hardcover"
      ? `https://hardcover.app/books/${encodeURIComponent(community.data?.slug || externalId)}`
      : `https://openlibrary.org/works/${encodeURIComponent(externalId)}`;
  const editions = book?.editions || [];
  const shown = editions.filter(
    (edition) => format === "all" || edition.medium === format,
  );
  return (
    <article className="reader-page">
      <Link to={fromSearch ? searchReturn : "/discover"} className="back-link">
        <ArrowLeft size={16} />{" "}
        {fromSearch ? "Back to search results" : "Back to Discover"}
      </Link>
      <Notice error={preview.error} />
      {preview.isPending && (
        <Loading label={`Finding book details on ${sourceName}…`} />
      )}
      {preview.error && (
        <button onClick={() => preview.refetch()} disabled={preview.isFetching}>
          Retry book details
        </button>
      )}
      {book && (
        <>
          {preview.data?.warning && (
            <p className="notice">{preview.data.warning}</p>
          )}
          <BookHero
            providerBook={{ provider, external_id: externalId }}
            title={book.title}
            authors={book.authors || []}
            work={work}
            cover={book.cover_url}
            caption={`${sourceName} edition`}
            eyebrow={`DISCOVER / ${sourceName.toUpperCase()}`}
            details={community.data}
            year={book.publication_year}
            language={book.language}
            editions={editions.length}
            editionsMore={book.editions_more}
            headingRef={heading}
            series={
              <>
                {!!book.series?.length && (
                  <div className="reader-series">
                    {book.series.map((series) =>
                      provider === "hardcover" ? (
                        <Link
                          key={series.external_id}
                          to={`/series/hardcover/${series.external_id}`}
                        >
                          {series.name}
                          {series.position ? ` · Book ${series.position}` : ""}
                        </Link>
                      ) : (
                        <span key={series.external_id}>{series.name}</span>
                      ),
                    )}
                  </div>
                )}
              </>
            }
          >
            <div className="status-row">
              {work ? (
                <>
                  <span
                    className={
                      work.availability.owned ? "status owned" : "status"
                    }
                  >
                    {work.availability.owned ? (
                      <>
                        <Check size={16} /> In library
                      </>
                    ) : watching ? (
                      "Waiting for release"
                    ) : (
                      "In your catalog"
                    )}
                  </span>
                  <LibraryFormatBadges work={work} />
                  {work.availability.stale && (
                    <span className="status">Last known availability</span>
                  )}
                </>
              ) : (
                <span className="muted">Not added to your catalog</span>
              )}
            </div>
            <div className="reader-actions">
              {canEdit && (
                <>
                  {unreleased ? (
                    <FollowRelease
                      canEdit={canEdit}
                      workId={work?.id}
                      idleLabel="Request on release day"
                      activeLabel="Waiting for release"
                      following={watching}
                      body={{
                        work_id: work?.id,
                        provider: provider === "hardcover" ? "hardcover" : null,
                        external_id:
                          provider === "hardcover" ? externalId : null,
                        title: book.title,
                        authors: book.authors || [],
                        cover_url: book.cover_url,
                        release_date: community.data?.release_date,
                        basis: community.data?.release_date
                          ? "work"
                          : "unknown",
                      }}
                    />
                  ) : (
                    <QuickAdd
                      workId={work?.id}
                      resolveWork={async () => {
                        const value = await save.mutateAsync();
                        return value.id;
                      }}
                    />
                  )}
                  <button
                    disabled={save.isPending}
                    onClick={() => choose("list")}
                  >
                    Add to list
                  </button>
                  {!unreleased && (
                    <button
                      disabled={save.isPending}
                      className="source-search-action"
                      onClick={() => choose("sources")}
                    >
                      Search sources
                    </button>
                  )}
                  {!work && !unreleased && (
                    <button
                      disabled={save.isPending}
                      onClick={() => choose("catalog")}
                    >
                      Add to catalog
                    </button>
                  )}
                </>
              )}
              {work && (
                <Link className="reader-action-link" to={`/books/${work.id}`}>
                  Manage book
                </Link>
              )}
              <div className="reader-outbound">
                <a href={externalUrl} target="_blank" rel="noreferrer">
                  <BookSourceIcon source={sourceName} />
                </a>
                <a
                  href={`https://www.goodreads.com/search?q=${encodeURIComponent(`${book.title} ${book.authors?.[0] || ""}`)}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  <BookSourceIcon source="Goodreads" />
                </a>
              </div>
            </div>
            {save.isPending && (
              <p role="status">Adding book to your catalog…</p>
            )}
            {canEdit && unreleased && (
              <p className="muted reader-action-note">
                {watching ? (
                  <>
                    A request is saved and searching starts on the release day.
                    It is listed in <Link to="/requests">Requests</Link>. The
                    book itself is under{" "}
                    <Link to="/library?view=saved">All saved titles</Link>, not
                    in your library copies.
                  </>
                ) : work ? (
                  <>
                    This book is saved in your catalog, but no release request
                    was created. Request on release day adds that request. Saved
                    titles are under{" "}
                    <Link to="/library?view=saved">All saved titles</Link>.
                  </>
                ) : (
                  <>
                    Request on release day saves this book and adds a request.
                    Searching starts on the release day. The request appears in{" "}
                    <Link to="/requests">Requests</Link>, and the book appears
                    under <Link to="/library?view=saved">All saved titles</Link>
                    .
                  </>
                )}
              </p>
            )}
            {canEdit && !work && canQuickAdd && !unreleased && (
              <p className="muted reader-action-note">
                Quick add downloads using your saved preferences and adds this
                title to your catalog.
              </p>
            )}
            {!canEdit && (
              <p className="muted">
                You have read-only access. Ask a member to request this book.
              </p>
            )}
            <Notice error={save.error} />
            {action === "catalog" && work && (
              <p className="success" role="status">
                Added to your catalog.
              </p>
            )}
          </BookHero>
          <DetailTabs tabs={tabs} selected={tab} label="Book sections" />
          {canEdit && work && (action === "request" || action === "list") && (
            <section
              ref={actionPanel}
              className="reader-action-panel"
              aria-label={action === "request" ? "Request book" : "Add to list"}
            >
              <button
                className="reader-close-action"
                onClick={() => setAction(null)}
              >
                Close {action === "request" ? "request" : "list"} form
              </button>
              {action === "request" ? (
                <Wanted
                  workId={work.id}
                  version={null}
                  clearVersion={() => {}}
                />
              ) : (
                <form
                  className="panel"
                  onSubmit={(event) => {
                    event.preventDefault();
                    addToList.mutate();
                  }}
                >
                  <h2>Add to a reading list</h2>
                  <ListChoice
                    label="Reading list"
                    value={listId}
                    disabled={addToList.isPending}
                    onChange={(value) => {
                      setListId(value);
                      addToList.reset();
                    }}
                  />
                  <button
                    className="primary"
                    disabled={!listId || addToList.isPending}
                  >
                    Save to list
                  </button>
                  <Notice error={addToList.error} />
                  {addToList.isSuccess && (
                    <p role="status" className="success">
                      Added to your list.
                    </p>
                  )}
                </form>
              )}
            </section>
          )}
          <div
            id="detail-tab-panel"
            role="tabpanel"
            aria-labelledby={`detail-tab-${tab}`}
            tabIndex={0}
          >
            {tab === "overview" && (
              <BookOverview
                description={book.description}
                subjects={book.subjects}
              />
            )}
            <section
              hidden={tab !== "editions"}
              id="editions"
              className="reader-section"
              aria-labelledby="editions-heading"
            >
              <div className="section-heading">
                <div>
                  <h2 id="editions-heading">Editions & formats</h2>
                  <p className="muted">
                    Explore print, ebook and audiobook editions.
                  </p>
                </div>
                <label>
                  Edition format
                  <select
                    value={format}
                    onChange={(event) => {
                      setFormat(event.target.value);
                      setEditionPage(1);
                    }}
                  >
                    <option value="all">All formats</option>
                    <option value="ebook">Ebook</option>
                    <option value="audio">Audiobook</option>
                    <option value="print">Print</option>
                    <option value="unknown">Other / unspecified</option>
                  </select>
                </label>
              </div>
              {shown.length ? (
                <div className="reader-editions">
                  {shown
                    .slice((editionPage - 1) * 20, editionPage * 20)
                    .map((edition) => (
                      <article
                        className="reader-edition"
                        key={edition.external_id}
                      >
                        {edition.cover_url ? (
                          <img
                            src={edition.cover_url}
                            alt=""
                            loading="lazy"
                            referrerPolicy="no-referrer"
                          />
                        ) : (
                          <BookOpen size={32} aria-hidden="true" />
                        )}
                        <div>
                          <span className="eyebrow">
                            {
                              {
                                audio: "AUDIOBOOK",
                                ebook: "EBOOK",
                                print: "PRINT",
                                unknown: "EDITION",
                              }[edition.medium || "unknown"]
                            }
                          </span>
                          <h3>{edition.title || book.title}</h3>
                          <p className="muted">
                            {[
                              edition.publisher,
                              edition.publication_year,
                              languageName(edition.language),
                            ]
                              .filter(Boolean)
                              .join(" · ") || "Publication details unavailable"}
                          </p>
                          {!!edition.narrators?.length && (
                            <p>Narrated by {edition.narrators.join(", ")}</p>
                          )}
                          {!!Object.keys(edition.identifiers || {}).length && (
                            <details>
                              <summary>ISBN & identifiers</summary>
                              {Object.entries(edition.identifiers || {}).map(
                                ([key, value]) => (
                                  <p key={key}>
                                    {key.replace("_", " ").toUpperCase()}:{" "}
                                    {value}
                                  </p>
                                ),
                              )}
                            </details>
                          )}
                        </div>
                      </article>
                    ))}
                </div>
              ) : (
                <p className="muted">
                  No{" "}
                  {format === "all"
                    ? ""
                    : format === "audio"
                      ? "audiobook "
                      : `${format} `}
                  editions are available in this catalog page.
                </p>
              )}
              <BookPagination
                page={editionPage}
                total={shown.length}
                onPage={setEditionPage}
              />
              {book.editions_more && (
                <p className="muted">
                  Showing the first {editions.length} editions.{" "}
                  <a href={externalUrl} target="_blank" rel="noreferrer">
                    Explore all editions on {sourceName} →
                  </a>
                </p>
              )}
            </section>
            {(tab === "authors" || tab === "reviews") && (
              <BookReaderDetails externalId={externalId} section={tab} />
            )}
          </div>
        </>
      )}
    </article>
  );
}
