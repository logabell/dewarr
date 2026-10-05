import { usePagedQuery } from "../hooks/usePagedQuery";
import ShelfViewAll from "./ShelfViewAll";
import BookSourceIcon from "./BookSourceIcon";
import ListDownloads from "./ListDownloads";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, Plus, Trophy } from "lucide-react";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { ContextLink } from "./NavigationContinuity";
import { Loading, Notice } from "../components";
import { GoodreadsCard } from "./GoodreadsDiscoveryBook";
import ShelfPagination from "./ShelfPagination";

export type Collection = components["schemas"]["CollectionCard"];
export type Entry = components["schemas"]["CollectionEntry"];
export const collectionPath = (id: string) =>
  `/discover/collections/${encodeURIComponent(id)}`;
const sourceNames: Record<string, string> = {
  goodreads: "Goodreads",
  nyt: "The New York Times",
  audible: "Audible",
  hugo: "Hugo Awards",
  nebula: "Nebula Awards",
  ala: "American Library Association",
  booker: "The Booker Prizes",
  pulitzer: "The Pulitzer Prizes",
  audie: "Audie Awards",
};
export const collectionSource = (value: string) => sourceNames[value] || value;
export const collectionCaption = (collection: Collection) =>
  [
    collection.year,
    collectionSource(collection.provider || "goodreads"),
    collection.region?.toUpperCase(),
  ]
    .filter(Boolean)
    .join(" · ");
export const genreLabel = (value: string) =>
  value.replaceAll("-", " ").replace(/^./, (s) => s.toUpperCase());

export function useCollections(
  filters: {
    kind?: "award" | "listopia" | "chart" | "collections" | "all";
    saved?: boolean;
    year?: number;
    genre?: string;
    category?: string;
    q?: string;
    page?: number;
    limit?: number;
  } = {},
) {
  return useQuery({
    queryKey: ["discovery-collections", filters],
    queryFn: async () =>
      result(
        await api.GET("/api/discovery/collections", {
          params: { query: filters },
        }),
      ),
    staleTime: 300_000,
  });
}

export function useFollow() {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: async ({
      collection,
      pinned,
      tracking,
    }: {
      collection: Collection;
      pinned: boolean;
      tracking: boolean;
    }) =>
      result(
        await api.PUT("/api/discovery/collections/{collection_id}/follow", {
          params: { path: { collection_id: collection.id } },
          body: { pinned, tracking },
        }),
      ),
    onSuccess: () => {
      cache.invalidateQueries({ queryKey: ["discovery-collections"] });
      cache.invalidateQueries({ queryKey: ["discovery-collection"] });
      cache.invalidateQueries({ queryKey: ["discovery-layout"] });
    },
  });
}

export function CollectionTile({ collection }: { collection: Collection }) {
  return (
    <ContextLink
      to={collectionPath(collection.id)}
      className="explore-collection"
    >
      <div className="explore-mosaic">
        {collection.covers.map((cover, i) => (
          <img
            key={i}
            src={`/api/catalog/cover-image?url=${encodeURIComponent(cover)}`}
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
          />
        ))}
        {!collection.covers.length && <Trophy size={36} />}
      </div>
      <div className="explore-collection-meta">
        <span>{collectionCaption(collection)}</span>
        {collection.pinned && <Check size={14} aria-label="On For you" />}
      </div>
      <h3>{collection.title}</h3>
      <p>
        {collection.count
          ? `${collection.count.toLocaleString()} ${collection.count === 1 ? "book" : "books"}`
          : "Explore current selection"}
        <ArrowRight size={16} />
      </p>
    </ContextLink>
  );
}

export function CollectionBooks({
  items,
  shelf = false,
  viewAll,
}: {
  items: Entry[];
  shelf?: boolean;
  viewAll?: string;
}) {
  return (
    <ul
      className={shelf ? "discovery-shelf" : "explore-books"}
      aria-label="Books"
    >
      {items.map((book) => (
        <li key={`${book.provider}:${book.external_id}`}>
          <GoodreadsCard book={book} />
          {(book.winner || (book.status && book.status !== "listed")) && (
            <span className="explore-winner">
              <Trophy size={12} />{" "}
              {book.winner ? "Winner" : genreLabel(book.status!)}
            </span>
          )}
          {book.rank != null && (
            <span className="curation-note">#{book.rank} on this list</span>
          )}
          {!!book.narrators?.length && (
            <p className="curation-note">
              Narrated by {book.narrators.join(", ")}
            </p>
          )}
          {!!book.contributors?.length && (
            <p className="curation-note">{book.contributors.join(", ")}</p>
          )}
          {book.subject && book.subject !== "work" && (
            <span className="curation-note">
              {book.subject === "recording"
                ? "Recording selection"
                : "Illustrated edition"}
            </span>
          )}
        </li>
      ))}
      {viewAll && <ShelfViewAll to={viewAll} />}
    </ul>
  );
}

export function CollectionRow({
  collection,
  canEdit = true,
}: {
  collection: Collection;
  canEdit?: boolean;
}) {
  const query = usePagedQuery({
    queryKey: ["discovery-collection", collection.id, "preview"],
    queryFn: async (page, signal) =>
      result(
        await api.GET("/api/discovery/collections/{collection_id}", {
          params: { path: { collection_id: collection.id }, query: { page } },
          signal,
        }),
      ),
    next: (last, pages) =>
      last.has_more && pages.reduce((n, p) => n + p.items.length, 0) < 100
        ? pages.length + 1
        : undefined,
    staleTime: 300_000,
  });
  return (
    <section
      className="discovery-section explore-row"
      aria-label={collection.title}
    >
      <div className="section-heading">
        <div>
          <p className="explore-kicker">{collectionCaption(collection)}</p>
          <h2>{collection.title}</h2>
        </div>
        <div className="button-row">
          {canEdit &&
            collection.medium !== "audio" &&
            collection.category !== "Illustration" && (
              <ListDownloads
                listId={collection.id}
                source="collection"
                name={collection.title}
                disabled={query.isPending || !query.data?.total}
              />
            )}
          <ContextLink
            className="shelf-action"
            to={collectionPath(collection.id)}
          >
            View all <ArrowRight size={15} />
          </ContextLink>
          <ShelfPagination
            page={1}
            hasMore={query.hasNextPage}
            busy={query.isFetching}
            infinite={{
              fetchNextPage: query.fetchNextPage,
              isFetchNextPageError: query.isFetchNextPageError,
              count: query.data?.items.length || 0,
            }}
            onPage={() => {}}
            label={collection.title}
          />
        </div>
      </div>
      <Notice error={query.error} />
      {query.isPending && <Loading />}
      {query.data && (
        <CollectionBooks
          shelf
          items={query.data.items.slice(0, 100)}
          viewAll={
            !query.hasNextPage ? collectionPath(collection.id) : undefined
          }
        />
      )}
    </section>
  );
}

export function CollectionActions({
  collection,
  canEdit,
}: {
  collection: Collection;
  canEdit: boolean;
}) {
  const follow = useFollow();
  const layout = useQuery({
    queryKey: ["discovery-layout"],
    queryFn: async () => result(await api.GET("/api/discovery/layout")),
  });
  const pinned =
    collection.pinned && !layout.data?.hidden?.includes(collection.id);
  return (
    <div className="collection-controls">
      <div className="button-row">
        {canEdit && (
          <>
            <button
              aria-pressed={pinned}
              className={pinned ? "" : "primary"}
              disabled={follow.isPending || layout.isPending || !!layout.error}
              onClick={() =>
                follow.mutate({
                  collection,
                  pinned: !pinned,
                  tracking: collection.tracking,
                })
              }
            >
              {pinned ? <Check size={16} /> : <Plus size={16} />}
              {pinned ? "On For you" : "Show on For you"}
            </button>
            {collection.medium !== "audio" &&
              collection.category !== "Illustration" && (
                <ListDownloads
                  listId={collection.id}
                  source="collection"
                  name={collection.title}
                  disabled={follow.isPending}
                />
              )}
            <button
              aria-pressed={collection.tracking}
              disabled={follow.isPending || layout.isPending || !!layout.error}
              onClick={() =>
                follow.mutate({
                  collection,
                  pinned,
                  tracking: !collection.tracking,
                })
              }
            >
              {collection.tracking ? "Following updates" : "Follow updates"}
            </button>
          </>
        )}
        <a
          className="shelf-action shelf-icon"
          aria-label={`Open collection on ${collectionSource(collection.provider || "goodreads")}`}
          title={`Open collection on ${collectionSource(collection.provider || "goodreads")}`}
          href={collection.source_url}
          target="_blank"
          rel="noopener noreferrer"
        >
          <BookSourceIcon
            source={collectionSource(collection.provider || "goodreads")}
          />
        </a>
      </div>
      {canEdit && (
        <p className="curation-note">
          Follow updates without downloading books.
        </p>
      )}
      <Notice error={follow.error} />
    </div>
  );
}
