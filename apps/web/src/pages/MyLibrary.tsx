import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "../components/InfiniteScroll";
import Catalog from "./Catalog";
import LibraryGroups from "../components/LibraryGroups";
import BookDialog from "../components/BookDialog";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { BookCard, Empty, Loading, Notice } from "../components";
import AssetMatchForm from "../components/AssetMatchForm";
import CollectionContents from "./CollectionContents";

type Asset = components["schemas"]["AssetView"];

function libraryApp(kind: string) {
  return kind === "grimmory" ? "Grimmory" : "Audiobookshelf";
}
type Medium = "any" | "ebook" | "audio";
type AssetState =
  | "any"
  | "present"
  | "stale"
  | "missing-suspected"
  | "missing-confirmed"
  | "scope-unavailable"
  | "moved";
type AssetSort = "title" | "recent";
export default function MyLibrary({
  admin,
  canEdit = false,
}: {
  admin: boolean;
  canEdit?: boolean;
}) {
  const [params, setParams] = useSearchParams();
  const q = (params.get("q") || "").slice(0, 300);
  const rawLibrary = params.get("library") || "";
  const libraryId =
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
      rawLibrary,
    )
      ? rawLibrary
      : "";
  const saved = params.get("view") === "saved";
  const groupView =
    params.get("view") === "authors" || params.get("view") === "series"
      ? (params.get("view") as "authors" | "series")
      : null;
  const medium: Medium =
    params.get("medium") === "ebook"
      ? "ebook"
      : params.get("medium") === "audio"
        ? "audio"
        : "any";
  const state: AssetState = "any";
  const sort: AssetSort = params.get("sort") === "recent" ? "recent" : "title";
  const rawOffset = Number(params.get("offset") || 0);
  const offset =
    Number.isSafeInteger(rawOffset) && rawOffset >= 0 ? rawOffset : 0;
  function change(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "offset") next.delete("offset");
    setParams(next);
  }
  const filtered = !!(q || libraryId || medium !== "any");
  const libraries = useQuery({
    queryKey: ["libraries"],
    refetchOnMount: "always",
    queryFn: async () => result(await api.GET("/api/library/libraries")),
  });
  return (
    <>
      <h1 className="sr-only">My Library</h1>
      <div className="page-view-toolbar">
        <nav className="library-scopes" aria-label="Library shelves">
          <Link
            to="/library"
            aria-current={!saved && !groupView ? "page" : undefined}
          >
            In my libraries
          </Link>
          <Link
            to="/library?view=authors"
            aria-current={groupView === "authors" ? "page" : undefined}
          >
            Authors
          </Link>
          <Link
            to="/library?view=series"
            aria-current={groupView === "series" ? "page" : undefined}
          >
            Series
          </Link>
          <Link
            to="/library?view=saved"
            aria-current={saved ? "page" : undefined}
          >
            All saved titles
          </Link>
        </nav>
        {admin && (
          <Link className="page-view-action" to="/settings#libraries">
            Manage connections
          </Link>
        )}
      </div>
      {groupView ? (
        <>
          <Notice error={libraries.error} />
          <LibraryGroups
            key={groupView}
            kind={groupView}
            libraries={libraries.data || []}
          />
        </>
      ) : saved ? (
        <Catalog canEdit={canEdit} />
      ) : (
        <>
          <Notice error={libraries.error} />
          <form
            className="library-search"
            aria-label="Search library books"
            onSubmit={(event) => {
              event.preventDefault();
              change(
                "q",
                String(new FormData(event.currentTarget).get("q") || "").trim(),
              );
            }}
          >
            <label>
              Search your library
              <input
                key={q}
                name="q"
                defaultValue={q}
                maxLength={300}
                type="search"
                placeholder="Title, author or narrator"
              />
            </label>
            <button type="submit">Search library</button>
          </form>
          <div className="library-filters library-shelf-filters">
            <label>
              Media
              <select
                value={medium}
                onChange={(event) => change("medium", event.target.value)}
              >
                <option value="any">Ebooks and audiobooks</option>
                <option value="ebook">Ebooks</option>
                <option value="audio">Audiobooks</option>
              </select>
            </label>
            <label>
              Library
              <select
                value={libraryId}
                onChange={(event) => change("library", event.target.value)}
              >
                <option value="">All accessible libraries</option>
                {libraries.data?.map((library) => (
                  <option key={library.id} value={library.id}>
                    {library.name}
                  </option>
                ))}
              </select>
            </label>

            <label>
              Sort books
              <select
                value={sort}
                onChange={(event) => change("sort", event.target.value)}
              >
                <option value="title">Title</option>
                <option value="recent">Recently observed</option>
              </select>
            </label>
          </div>
          {(filtered || sort !== "title" || offset > 0) && (
            <button type="button" onClick={() => setParams({})}>
              Reset library view
            </button>
          )}
          {sort === "recent" && (
            <p className="muted">
              Newest first observed by this app. Initial sync includes older
              books; repeated syncs do not reset this order.
            </p>
          )}
          <LibraryBooks
            q={q}
            medium={medium}
            state={state}
            sort={sort}
            libraryId={libraryId}
          />
        </>
      )}
    </>
  );
}

function LibraryBooks({
  q,
  medium,
  state,
  sort,
  libraryId,
}: {
  q: string;
  medium: Medium;
  state: AssetState;
  sort: AssetSort;
  libraryId: string;
}) {
  const books = usePagedQuery({
    queryKey: ["library-books", q, medium, state, sort, libraryId],
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/library/books", {
          signal,
          params: {
            query: {
              q,
              medium,
              state,
              sort,
              library_id: libraryId || undefined,
              offset,
              limit: 40,
            },
          },
        }),
      ),
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.total ? count : undefined;
    },
    refetchInterval: 10_000,
    retry: false,
  });
  return (
    <section aria-label="Library books">
      <Notice error={books.error} />
      {books.isPending && <Loading />}
      {books.error && (
        <button onClick={() => books.refetch()} disabled={books.isFetching}>
          Retry library books
        </button>
      )}
      {books.data && (
        <>
          <div className="section-heading">
            <h2>{q ? "Matching books" : "Your bookshelf"}</h2>
            <span className="count">
              {books.data.total} {books.data.total === 1 ? "book" : "books"}
            </span>
          </div>
          {books.data.items.length ? (
            <div className="book-grid">
              {books.data.items.map((work) => (
                <BookCard key={work.id} work={work} medium={medium} />
              ))}
            </div>
          ) : (
            <Empty title="No library books to show">
              Try another filter or <Link to="/search">find a book</Link>.
            </Empty>
          )}
          <InfiniteScroll query={books} />
        </>
      )}
    </section>
  );
}

export function LibraryAssets({
  workId,
  compact = false,
  libraryId,
  review = false,
  admin = false,
  q = "",
  medium = "any",
  state = "any",
  sort = "title",
  filtered = false,
}: {
  workId?: string;
  compact?: boolean;
  libraryId?: string;
  review?: boolean;
  admin?: boolean;
  q?: string;
  medium?: Medium;
  state?: AssetState;
  sort?: AssetSort;
  filtered?: boolean;
}) {
  const [fileDetails, setFileDetails] = useState<Asset | null>(null);
  const [matching, setMatching] = useState<Asset | null>(null);
  const [collection, setCollection] = useState<Asset | null>(null);
  const assets = usePagedQuery({
    queryKey: ["assets", workId, libraryId, review, q, medium, state, sort],
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/library/assets", {
          signal,
          params: {
            query: {
              work_id: workId,
              library_id: libraryId || undefined,
              needs_review: review,
              q,
              medium,
              state,
              sort,
              offset,
              limit: 40,
            },
          },
        }),
      ),
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.total ? count : undefined;
    },
    refetchInterval: 15000,
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
  return (
    <section aria-label="Library copies">
      <Notice error={assets.error} />
      {assets.error && (
        <button disabled={assets.isFetching} onClick={() => assets.refetch()}>
          Retry library copies
        </button>
      )}
      {assets.data && (
        <p className="muted" role="status">
          {assets.data.total}{" "}
          {assets.data.total === 1 ? "library copy" : "library copies"}
          {filtered
            ? assets.data.total === 1
              ? " matches this view"
              : " match this view"
            : ""}
        </p>
      )}
      {fileDetails && (
        <BookDialog title="Files & location" close={() => setFileDetails(null)}>
          <h2>{fileDetails.library_name}</h2>
          <p className="muted book-dialog-book-title">{fileDetails.title}</p>
          {fileDetails.files?.length ? (
            <ul className="library-files">
              {fileDetails.files.map((file, index) => (
                <li key={`${file.path}:${index}`}>
                  <code>{file.path}</code>
                  <span className="muted">
                    {file.format.toUpperCase()}
                    {file.size != null
                      ? ` · ${(file.size / 1024 / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 })} MB`
                      : ""}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">
              File locations have not been supplied by this library yet.
            </p>
          )}
          {fileDetails.last_seen_at && (
            <p className="muted">
              Last seen {new Date(fileDetails.last_seen_at).toLocaleString()}
            </p>
          )}
          <a href={fileDetails.open_url} target="_blank" rel="noreferrer">
            Open in {libraryApp(fileDetails.server_kind)} →
          </a>
        </BookDialog>
      )}
      {!assets.error && matching && (
        <AssetMatchForm asset={matching} close={() => setMatching(null)} />
      )}
      {!assets.error && collection && (
        <CollectionContents
          asset={collection}
          close={() => setCollection(null)}
        />
      )}
      {assets.isPending ? (
        <Loading />
      ) : assets.data?.items.length ? (
        compact ? (
          <div className="book-table-scroll">
            <table className="book-data-table library-copy-table">
              <thead>
                <tr>
                  <th>Edition / recording</th>
                  <th>Library</th>
                  <th>Files</th>
                  <th>Availability</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {assets.data.items.map((asset) => (
                  <tr key={asset.id}>
                    <td>
                      <strong>
                        {asset.medium === "audio" ? "Audiobook" : "Ebook"}
                      </strong>
                      <small>{asset.title}</small>
                      {asset.medium === "audio" && (
                        <small>
                          {asset.narrators.length
                            ? `Narrated by ${asset.narrators.join(", ")}`
                            : "Narrator not supplied"}
                        </small>
                      )}
                      <small>
                        {asset.formats
                          .map((value) => value.toUpperCase())
                          .join(" / ")}
                      </small>
                    </td>
                    <td>
                      {asset.library_name}
                      {asset.collection && <small>Part of a collection</small>}
                    </td>
                    <td>
                      <button
                        className="text-button"
                        onClick={() => setFileDetails(asset)}
                      >
                        {asset.files?.length || 0}{" "}
                        {asset.files?.length === 1 ? "file" : "files"} ·
                        Location
                      </button>
                    </td>
                    <td>
                      <span
                        className={
                          asset.state === "present" && asset.full_content
                            ? "status owned"
                            : "status"
                        }
                      >
                        {(
                          {
                            present: asset.full_content
                              ? "Available"
                              : "Needs verification",
                            stale: "Last known",
                            "missing-suspected": "Checking",
                            "missing-confirmed": "Missing",
                            "scope-unavailable": "Access changed",
                            moved: "Moved",
                          } as Record<string, string>
                        )[asset.state] || asset.state.replaceAll("-", " ")}
                      </span>
                    </td>
                    <td>
                      <div className="table-actions">
                        <a
                          href={asset.open_url}
                          target="_blank"
                          rel="noreferrer"
                          aria-label={`Open in ${libraryApp(asset.server_kind)}`}
                        >
                          Open ↗
                        </a>
                        {admin && (
                          <button
                            className="text-button"
                            onClick={() => {
                              setCollection(null);
                              setMatching(asset);
                            }}
                          >
                            Correct match
                          </button>
                        )}
                        {admin && asset.collection && (
                          <button
                            className="text-button"
                            onClick={() => {
                              setMatching(null);
                              setCollection(asset);
                            }}
                          >
                            Collection
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="activity-list">
            {assets.data.items.map((asset) => (
              <article className="activity-row library-copy" key={asset.id}>
                <div className="grow">
                  <h2>
                    {asset.collection_work_id || asset.work_ids.length === 1 ? (
                      <Link
                        to={`/books/${asset.collection_work_id || asset.work_ids[0]}`}
                      >
                        {asset.title}
                      </Link>
                    ) : (
                      asset.title
                    )}
                  </h2>
                  {!!asset.authors?.length && (
                    <p className="muted">{asset.authors.join(", ")}</p>
                  )}
                  <p>
                    {asset.medium === "audio" ? "Audiobook" : "Ebook"}
                    {asset.narrators.length
                      ? ` · ${asset.collection ? "Collection narrators: " : ""}${asset.narrators.join(", ")}`
                      : ""}{" "}
                    ·{" "}
                    {asset.formats
                      .map((format) => format.toUpperCase())
                      .join(" / ")}
                  </p>
                  <p className="muted">
                    {asset.library_name} ·{" "}
                    {asset.full_content
                      ? asset.collection
                        ? "In collection · Verified complete books"
                        : "Full book"
                      : "Supplementary or needs verification"}
                  </p>
                  {workId && (
                    <details className="library-file-details">
                      <summary>
                        Files & location
                        {asset.files?.length
                          ? ` · ${asset.files.length} ${asset.files.length === 1 ? "file" : "files"}`
                          : ""}
                      </summary>
                      {asset.files?.length ? (
                        <ul className="library-files">
                          {asset.files.map((file, index) => (
                            <li key={`${file.path}:${index}`}>
                              <code>{file.path}</code>
                              <span className="muted">
                                {file.format.toUpperCase()}
                                {file.size != null
                                  ? ` · ${(file.size / 1024 / 1024).toLocaleString(undefined, { maximumFractionDigits: 1 })} MB`
                                  : ""}
                              </span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="muted">
                          File locations will appear after a library sync
                          supplies them.
                        </p>
                      )}
                      {asset.last_seen_at && (
                        <p className="muted">
                          Last seen{" "}
                          <time dateTime={asset.last_seen_at}>
                            {new Date(asset.last_seen_at).toLocaleString()}
                          </time>
                        </p>
                      )}
                    </details>
                  )}
                  {asset.collection && (
                    <ul aria-label="Collection contents">
                      {asset.contents?.map((book) => (
                        <li key={book.work_id}>
                          <Link to={`/books/${book.work_id}`}>
                            {book.title}
                          </Link>
                          {!book.verified && " · Needs verification"}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
                <div className="activity-meta">
                  <span className="status">
                    {(
                      {
                        present: "Available",
                        stale: "Last known availability",
                        "missing-suspected": "Checking availability",
                        "missing-confirmed": "Missing",
                        "scope-unavailable": "Access changed",
                        moved: "Moved",
                      } as Record<string, string>
                    )[asset.state] || asset.state.replaceAll("-", " ")}
                  </span>
                  {asset.match_status === "needs-review" && (
                    <span className="status">Needs matching</span>
                  )}
                  <div className="button-row">
                    <a href={asset.open_url} target="_blank" rel="noreferrer">
                      Open in {libraryApp(asset.server_kind)}
                    </a>
                    {admin && (
                      <button
                        onClick={() => {
                          setMatching(null);
                          setCollection(asset);
                        }}
                      >
                        Review collection contents
                      </button>
                    )}
                    {admin && (
                      <button
                        onClick={() => {
                          setCollection(null);
                          setMatching(asset);
                        }}
                      >
                        Correct match
                      </button>
                    )}
                  </div>
                </div>
              </article>
            ))}
          </div>
        )
      ) : (
        !assets.isError && (
          <Empty
            title={
              review && !filtered
                ? "All caught up"
                : "No library copies to show"
            }
          >
            {filtered
              ? "No copies match these filters. Try another title, author, narrator or media type."
              : review
                ? "No items need matching in this view."
                : "A completed library sync brings accessible books and recordings here."}
          </Empty>
        )
      )}
      <InfiniteScroll query={assets} />
    </section>
  );
}
