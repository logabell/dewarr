import BookSourceIcon from "../components/BookSourceIcon";
import QuickAdd from "../components/QuickAdd";
import BookGrouping from "../components/BookGrouping";
import LibraryFormatBadges from "../components/LibraryFormatBadges";
import PartSets from "../components/CombineParts";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, Settings2 } from "lucide-react";
import {
  Link,
  Navigate,
  useLocation,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { api, result } from "../api/client";
import {
  BookHero,
  BookOverview,
  releaseIsAhead,
} from "../components/BookPresentation";
import FollowRelease, { useReleaseWatch } from "../components/FollowRelease";
import BookReaderDetails, {
  useLocalBookMetadata,
  useReaderDetails,
} from "../components/BookReaderDetails";
import { LibraryAssets } from "./MyLibrary";
import { Loading, Notice } from "../components";
import BookMetadata from "./BookMetadata";
import WorkMerge from "./WorkMerge";
import Wanted, { type WantedVersion } from "./Wanted";
import ListChoice from "./ListChoice";
import BookDownloads from "../components/BookDownloads";
import BookDetailsStatus from "../components/BookDetailsStatus";
import BookDialog from "../components/BookDialog";
import CatalogEditions from "../components/CatalogEditions";
const BookSources = lazy(() => import("./BookSources"));
const RelatedBooks = lazy(() => import("../components/RelatedBooks"));
const tabs = [
  ["overview", "Overview"],
  ["library", "Library copies"],
  ["editions", "Editions"],
  ["authors", "Authors"],
  ["reviews", "Reviews"],
  ["downloads", "Downloads"],
  ["sources", "Sources"],
] as const;

export default function BookDetail({
  canEdit,
  admin,
}: {
  canEdit: boolean;
  admin: boolean;
}) {
  const { id = "" } = useParams();
  return <BookDetailContent key={id} id={id} canEdit={canEdit} admin={admin} />;
}
function BookDetailContent({
  id,
  canEdit,
  admin,
}: {
  id: string;
  canEdit: boolean;
  admin: boolean;
}) {
  const [params] = useSearchParams();
  const location = useLocation();
  const legacy =
    location.hash.slice(1) === "library-copies"
      ? "library"
      : location.hash.slice(1);
  const selected = params.get("tab") || legacy || "overview";
  const tab =
    selected === "manage" || tabs.some(([key]) => key === selected)
      ? selected
      : "overview";
  const cache = useQueryClient();
  const [action, setAction] = useState<"request" | "list" | null>(null);
  const [listId, setListId] = useState("");
  const [wantedVersion, setWantedVersion] = useState<WantedVersion | null>(
    null,
  );
  const heading = useRef<HTMLHeadingElement>(null);
  const book = useQuery({
    queryKey: ["work", id],
    queryFn: async () =>
      result(
        await api.GET("/api/catalog/works/{work_id}", {
          params: { path: { work_id: id } },
        }),
      ),
    staleTime: 0,
    gcTime: 0,
    refetchInterval: 15_000,
  });
  const metadata = useLocalBookMetadata(id);
  const acceptedHardcover = metadata.data?.sources.find(
    (item) => item.provider === "hardcover",
  );
  const match = useQuery({
    queryKey: ["work-reader-match", id],
    enabled: !!metadata.data && !acceptedHardcover,
    queryFn: async () =>
      result(
        await api.GET("/api/metadata/works/{work_id}/reader-match", {
          params: { path: { work_id: id } },
        }),
      ),
    staleTime: 300_000,
    retry: false,
  });
  const matchedBook = !acceptedHardcover ? match.data?.book : undefined;
  const source =
    acceptedHardcover ||
    (matchedBook
      ? {
          provider: matchedBook.provider,
          external_id: matchedBook.external_id,
          book: matchedBook,
          cover_url: matchedBook.cover_url,
          series: matchedBook.series,
        }
      : metadata.data?.sources[0]);
  const hardcover = acceptedHardcover || (matchedBook ? source : undefined);
  const matching = !!metadata.data && !acceptedHardcover && match.isPending;
  const community = useReaderDetails(hardcover?.external_id);
  const provider = useQuery({
    queryKey: ["provider-book", source?.provider, source?.external_id],
    enabled: !!source && !matchedBook,
    queryFn: async () =>
      result(
        await api.GET("/api/metadata/books/{provider}/{external_id}", {
          params: {
            path: {
              provider: source!.provider,
              external_id: source!.external_id,
            },
          },
        }),
      ),
    staleTime: 300_000,
    retry: false,
  });
  const add = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/lists/{list_id}/entries", {
          params: { path: { list_id: listId } },
          body: { work_id: id },
        }),
      ),
    onSuccess: () => {
      cache.invalidateQueries({ queryKey: ["lists"] });
    },
  });
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
  }, [book.data?.id]);
  const watch = useReleaseWatch(
    book.data?.id,
    !!book.data &&
      releaseIsAhead(community.data?.release_date, book.data.publication_year),
  );
  const watching =
    watch.data?.state === "waiting" || watch.data?.state === "wanted";
  if (book.isPending) return <Loading />;
  if (book.error || !book.data) return <Notice error={book.error} />;
  const work = book.data;
  if (work.id !== id)
    return (
      <Navigate
        to={`/books/${work.id}${location.search}${location.hash}`}
        replace
      />
    );
  // Display accepted provider details when local fields are empty, preserving explicit edits.
  const locked = (field: string) =>
    (metadata.data?.fields[field] as { locked?: boolean } | undefined)?.locked;
  const description =
    work.description?.trim() ||
    (locked("description")
      ? work.description
      : provider.data?.book?.description || source?.book?.description);
  const year =
    work.publication_year ??
    (locked("publication_year")
      ? null
      : provider.data?.book?.publication_year ||
        source?.book?.publication_year);
  const unreleased = releaseIsAhead(community.data?.release_date, year);
  const cover = locked("cover_url")
    ? undefined
    : provider.data?.book?.cover_url || source?.cover_url;
  const series = source?.series || [];
  const request = (version: WantedVersion) => {
    setWantedVersion(version);
    setAction("request");
  };
  const href = (value: string) => `/books/${id}?tab=${value}`;
  return (
    <article className="reader-page catalog-reader catalog-workspace">
      <div className="book-topbar">
        <Link to="/library" className="back-link">
          <ArrowLeft size={16} /> Back to My Library
        </Link>
        <Link
          className={`book-manage-link ${tab === "manage" ? "active" : ""}`}
          to={href("manage")}
          aria-label="Book metadata"
        >
          <Settings2 size={17} /> Book metadata
        </Link>
      </div>
      <BookHero
        title={work.title}
        authors={work.authors}
        work={work}
        cover={cover}
        caption={
          work.availability.owned
            ? "In your library"
            : watching
              ? "Waiting for release"
              : "Saved in your catalog"
        }
        eyebrow={work.availability.owned ? "YOUR LIBRARY" : "YOUR CATALOG"}
        details={community.data}
        year={year}
        language={work.language || source?.book?.language}
        editions={
          matchedBook?.editions?.length ?? metadata.data?.versions_total
        }
        editionsMore={
          matchedBook?.editions_more ??
          metadata.data?.sources.some((item) => item.editions_more)
        }
        headingRef={heading}
        series={
          !!series.length && (
            <div className="reader-series">
              {series.map((item) =>
                source?.provider === "hardcover" ? (
                  <Link
                    key={item.external_id}
                    to={`/series/hardcover/${item.external_id}`}
                  >
                    {item.name}
                    {item.position ? ` · Book ${item.position}` : ""}
                  </Link>
                ) : (
                  <span key={item.external_id}>{item.name}</span>
                ),
              )}
            </div>
          )
        }
      >
        <div className="status-row">
          {work.availability.owned && (
            <span className="status owned">
              <Check size={15} /> In library
            </span>
          )}
          {unreleased && !work.availability.owned && (
            <span className="status">
              {watching ? "Waiting for release" : "In your catalog"}
            </span>
          )}
          <LibraryFormatBadges work={work} />
          {work.availability.stale && (
            <span className="muted">Last known availability</span>
          )}
        </div>
        <div className="reader-actions">
          {work.availability.owned && (
            <Link className="reader-action-link" to={href("library")}>
              View library copies
            </Link>
          )}
          {canEdit &&
            (unreleased ? (
              <FollowRelease
                canEdit={canEdit}
                following={watching}
                workId={work.id}
                idleLabel="Request on release day"
                activeLabel="Waiting for release"
                body={{
                  work_id: work.id,
                  provider: hardcover ? "hardcover" : null,
                  external_id: hardcover?.external_id,
                  title: work.title,
                  authors: work.authors,
                  cover_url: cover,
                  release_date: community.data?.release_date,
                  basis: community.data?.release_date ? "work" : "unknown",
                }}
              />
            ) : (
              <QuickAdd workId={work.id} />
            ))}
          {canEdit && !unreleased && (
            <Link
              className="reader-action-link source-search-action"
              to={href("sources")}
            >
              Search sources
            </Link>
          )}
          {canEdit && (
            <button
              onClick={() => {
                add.reset();
                setAction("list");
              }}
            >
              Add to reading list
            </button>
          )}
          <div className="reader-outbound">
            {hardcover && (
              <a
                href={`https://hardcover.app/books/${encodeURIComponent(community.data?.slug || hardcover.external_id)}`}
                target="_blank"
                rel="noreferrer"
              >
                <BookSourceIcon source="Hardcover" />
              </a>
            )}
            {source?.provider === "openlibrary" && (
              <a
                href={`https://openlibrary.org/works/${encodeURIComponent(source.external_id)}`}
                target="_blank"
                rel="noreferrer"
              >
                <BookSourceIcon source="Open Library" />
              </a>
            )}
            <a
              href={`https://www.goodreads.com/search?q=${encodeURIComponent(`${work.title} ${work.authors[0] || ""}`)}`}
              target="_blank"
              rel="noreferrer"
            >
              <BookSourceIcon source="Goodreads" />
            </a>
          </div>
        </div>
        {canEdit && unreleased && !work.availability.owned && (
          <p className="muted reader-action-note">
            {watching ? (
              <>
                A request is saved and searching starts on the release day. It
                is listed in <Link to="/requests">Requests</Link>. This book is
                under <Link to="/library?view=saved">All saved titles</Link>,
                not in your library copies. Waiting for release stops that
                request.
              </>
            ) : (
              <>
                Saved in your catalog only. It is not in your library, and it is
                not in Requests until you request the release day. Find it under{" "}
                <Link to="/library?view=saved">All saved titles</Link>.
              </>
            )}
          </p>
        )}
        {(metadata.isPending ||
          matching ||
          (hardcover && community.isPending)) && (
          <p className="muted book-detail-loading" role="status">
            Loading book details…
          </p>
        )}
      </BookHero>
      {tab !== "manage" && (
        <BookDetailsStatus
          reason={match.data?.reason}
          workId={id}
          error={
            metadata.error ||
            (!acceptedHardcover ? match.error : null) ||
            provider.error ||
            community.error
          }
          unmatched={
            !acceptedHardcover &&
            match.data?.status === "unmatched" &&
            !match.error
          }
          busy={
            metadata.isFetching ||
            match.isFetching ||
            provider.isFetching ||
            community.isFetching
          }
          retry={() => {
            if (metadata.error) metadata.refetch();
            if (!acceptedHardcover && match.error) match.refetch();
            if (source && provider.error) provider.refetch();
            if (hardcover && community.error) community.refetch();
          }}
        />
      )}
      <nav
        className="reader-nav book-tabs"
        role="tablist"
        aria-label="Book sections"
        onKeyDown={(event) => {
          if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
            return;
          const links = [
            ...event.currentTarget.querySelectorAll<HTMLAnchorElement>(
              '[role="tab"]',
            ),
          ];
          const index = links.indexOf(
            document.activeElement as HTMLAnchorElement,
          );
          const next =
            event.key === "Home"
              ? 0
              : event.key === "End"
                ? links.length - 1
                : (index +
                    (event.key === "ArrowRight" ? 1 : -1) +
                    links.length) %
                  links.length;
          event.preventDefault();
          links[next].focus();
          links[next].click();
        }}
      >
        {tabs.map(([key, label]) => (
          <Link
            key={key}
            id={`tab-${key}`}
            role="tab"
            aria-selected={tab === key}
            tabIndex={
              tab === key || (tab === "manage" && key === "overview") ? 0 : -1
            }
            aria-controls="book-tab-panel"
            to={href(key)}
          >
            {label}
          </Link>
        ))}
      </nav>
      <div
        id="book-tab-panel"
        role="tabpanel"
        aria-labelledby={tab === "manage" ? undefined : `tab-${tab}`}
        aria-label={tab === "manage" ? "Book metadata" : undefined}
        tabIndex={0}
      >
        {tab === "overview" && (
          <>
            {!description &&
            (metadata.isPending ||
              matching ||
              (source && !matchedBook && provider.isPending)) ? (
              <Loading />
            ) : (
              <BookOverview
                description={description}
                subjects={
                  provider.data?.book?.subjects || source?.book?.subjects
                }
              />
            )}
            <Suspense fallback={<Loading />}>
              <RelatedBooks workId={id} />
            </Suspense>
          </>
        )}
        {tab === "library" && (
          <section className="book-tab-section">
            <div className="book-tab-heading">
              <h2>Library copies</h2>
            </div>
            <div className="button-row" aria-label="Library formats">
              <Link to={href("library")}>All formats</Link>
              {work.availability.ebook && (
                <Link to={`${href("library")}&format=ebook`}>Ebooks</Link>
              )}
              {work.availability.audio && (
                <Link to={`${href("library")}&format=audio`}>Audiobooks</Link>
              )}
            </div>
            {admin && <PartSets workId={id} />}
            <LibraryAssets
              key={params.get("format") || "any"}
              workId={id}
              admin={admin}
              compact
              medium={
                params.get("format") === "audio"
                  ? "audio"
                  : params.get("format") === "ebook"
                    ? "ebook"
                    : "any"
              }
            />
          </section>
        )}
        {tab === "editions" && (
          <CatalogEditions
            admin={admin}
            work={work}
            readerBook={matchedBook}
            request={canEdit ? request : undefined}
          />
        )}
        {(tab === "authors" || tab === "reviews") &&
          (matching ? (
            <Loading />
          ) : hardcover ? (
            <BookReaderDetails
              externalId={hardcover.external_id}
              section={tab}
            />
          ) : (
            <section className="book-tab-section">
              <h2>
                {tab === "authors" ? "About the authors" : "Reader reviews"}
              </h2>
              <p className="muted">
                Match this book to Hardcover to show{" "}
                {tab === "authors"
                  ? "author biographies"
                  : "ratings and reviews"}{" "}
                here.
              </p>
              <Link to={href("manage")}>Book metadata →</Link>
            </section>
          ))}
        {tab === "downloads" && <BookDownloads workId={id} />}
        {tab === "sources" && (
          <section className="book-tab-section" aria-label="Download sources">
            <Suspense fallback={<Loading />}>
              <BookSources
                key={`${id}:${params.get("request") || ""}`}
                work={work}
                canAcquire={canEdit}
              />
            </Suspense>
          </section>
        )}
        {tab === "manage" && (
          <section className="book-tab-section book-management">
            <BookMetadata work={work} admin={admin} toolsOnly />
            <BookGrouping work={work} admin={admin} />
            {admin && (
              <details className="book-management-advanced">
                <summary>Merge a duplicate book</summary>
                <WorkMerge work={work} />
              </details>
            )}
          </section>
        )}
      </div>
      {canEdit && action && (
        <BookDialog
          title={action === "list" ? "Add to reading list" : "Request book"}
          close={() => setAction(null)}
        >
          {action === "request" ? (
            <Wanted
              key={wantedVersion?.id || id}
              workId={wantedVersion?.work_id || id}
              version={wantedVersion}
              clearVersion={() => setWantedVersion(null)}
            />
          ) : (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                add.mutate();
              }}
            >
              <h2>Add to a reading list</h2>
              <p className="muted book-dialog-book-title">{work.title}</p>
              <ListChoice
                value={listId}
                label="Reading list"
                disabled={add.isPending}
                onChange={(value) => {
                  setListId(value);
                  add.reset();
                }}
              />
              <Notice error={add.error} />
              <button className="primary" disabled={!listId || add.isPending}>
                Add to list
              </button>
              {add.isSuccess && (
                <p className="success" role="status">
                  Added to your list.
                </p>
              )}
            </form>
          )}
        </BookDialog>
      )}
    </article>
  );
}
