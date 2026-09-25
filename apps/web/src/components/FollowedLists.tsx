import { browseCache } from "../queryPolicies";
import ShelfViewAll from "./ShelfViewAll";
import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "./InfiniteScroll";
import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, Check, Plus, RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import EnrichedBookCard from "./EnrichedBookCard";
import ListDownloads from "./ListDownloads";
import ShelfPagination from "./ShelfPagination";
import { randomUUID } from "../randomUUID";

function emptyShelfCopy(
  provider: string | undefined,
  count: number,
  settled: boolean,
  failed: boolean,
) {
  if (failed) return "This list needs attention.";
  if (provider && settled && count === 0) return "No books on this list.";
  if (provider) return "Books will appear after the first update.";
  return "Add books from their book pages.";
}

export default function FollowedLists({
  home = true,
  canEdit = true,
}: {
  home?: boolean;
  canEdit?: boolean;
}) {
  const allLists = usePagedQuery({
    queryKey: ["lists", "discover"],
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/lists/page", {
          params: { query: { offset, limit: 4 } },
          signal,
        }),
      ),
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.total ? count : undefined;
    },
  });
  const layout = useQuery({
    queryKey: ["discovery-layout"],
    queryFn: async () => result(await api.GET("/api/discovery/layout")),
  });
  const visible = allLists.data?.items.filter(
    (list) => !home || !layout.data?.hidden?.includes(`personal:${list.id}`),
  );
  const total = allLists.data?.total;
  return (
    <section aria-label="Your followed lists" className="explore-personal">
      <Notice error={allLists.error || layout.error} />
      {allLists.isPending && <Loading />}
      {total === 0 && !home && (
        <div className="explore-empty">
          <p>Your reading lists belong here.</p>
          <Link className="back-link" to="/settings#reading">
            Connect Goodreads, StoryGraph, or Hardcover <ArrowRight size={15} />
          </Link>
        </div>
      )}
      {visible?.map((list) => (
        <PersonalRow
          key={list.id}
          list={list}
          manage={!home}
          canEdit={canEdit && list.editable}
        />
      ))}
      <InfiniteScroll query={allLists} />
    </section>
  );
}
export function PersonalRow({
  list,
  manage,
  canEdit,
}: {
  list: components["schemas"]["ListView"];
  manage: boolean;
  canEdit: boolean;
}) {
  const syncKey = useRef(randomUUID());
  const shelf = useRef<HTMLUListElement>(null);
  const cache = useQueryClient();
  const books = usePagedQuery({
    queryKey: ["discovery-personal", list.id],
    ...browseCache,
    queryFn: async (page, signal) =>
      result(
        await api.GET("/api/lists/{list_id}", {
          signal,
          params: {
            path: { list_id: list.id },
            query: { limit: 16, offset: (page - 1) * 16, sort: "newest" },
          },
        }),
      ),
    staleTime: 60_000,
    next: (last, pages) =>
      pages.reduce((n, p) => n + p.items.length, 0) < Math.min(last.count, 100)
        ? pages.length + 1
        : undefined,
  });
  const layout = useQuery({
    queryKey: ["discovery-layout"],
    queryFn: async () => result(await api.GET("/api/discovery/layout")),
  });
  const subscription = useQuery({
    queryKey: ["list-subscription", list.id],
    enabled: canEdit,
    queryFn: async () =>
      result(
        await api.GET("/api/lists/{list_id}/subscription", {
          params: { path: { list_id: list.id } },
        }),
      ),
    refetchInterval: (q) =>
      ["queued", "running"].includes(q.state.data?.state || "") ? 1500 : 30_000,
  });
  const lastSuccess = subscription.data?.last_success_at;
  useEffect(() => {
    if (lastSuccess) {
      if (shelf.current) shelf.current.scrollLeft = 0;
      void cache.invalidateQueries({
        queryKey: ["discovery-personal", list.id],
        ...browseCache,
      });
      void cache.invalidateQueries({ queryKey: ["lists"] });
    }
  }, [lastSuccess, cache, list.id]);
  const refresh = useMutation({
    mutationFn: async () => {
      if (!subscription.data) return books.refetch();
      return result(
        await api.POST("/api/lists/{list_id}/subscription/sync", {
          params: {
            path: { list_id: list.id },
            header: { "idempotency-key": syncKey.current },
          },
        }),
      );
    },
    onSuccess: () => {
      syncKey.current = randomUUID();
      void cache.invalidateQueries({
        queryKey: ["list-subscription", list.id],
      });
    },
  });
  const syncing =
    refresh.isPending ||
    ["queued", "running"].includes(subscription.data?.state || "");
  const provider = subscription.data?.provider;
  const key = `personal:${list.id}`;
  const pinned = !layout.data?.hidden?.includes(key);
  const pin = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/discovery/layout", {
          body: {
            order: layout.data?.order || [],
            hidden: pinned
              ? [...(layout.data?.hidden || []), key]
              : (layout.data?.hidden || []).filter((v) => v !== key),
          },
        }),
      ),
    onSuccess: (value) => cache.setQueryData(["discovery-layout"], value),
  });
  const items = books.data?.items.slice(0, 100) || [];
  return (
    <section
      className="discovery-section"
      aria-label={`${list.name} followed list`}
    >
      <div className="section-heading">
        <div>
          <p className="explore-kicker">
            {provider
              ? `${provider === "goodreads" ? "Goodreads" : provider === "storygraph" ? "StoryGraph" : "Hardcover"} · `
              : ""}
            Your list · {books.data?.count ?? list.count} books
          </p>
          <h2>{list.name}</h2>
        </div>
        <div className="button-row">
          {manage && canEdit && (
            <button
              disabled={pin.isPending || !layout.data}
              aria-pressed={pinned}
              onClick={() => pin.mutate()}
              aria-label={`${pinned ? "Hide" : "Show"} ${list.name} on For you`}
            >
              {pinned ? <Check size={15} /> : <Plus size={15} />}
              {pinned ? "On For you" : "Show on For you"}
            </button>
          )}
          {canEdit && (
            <>
              <button
                className="shelf-action shelf-icon"
                aria-label={`Refresh ${list.name}`}
                title="Pull in latest books"
                disabled={
                  syncing ||
                  subscription.isPending ||
                  !!subscription.error ||
                  subscription.data?.enabled === false
                }
                onClick={() => refresh.mutate()}
              >
                <RefreshCw
                  size={16}
                  className={syncing ? "list-refresh-spinning" : undefined}
                />
              </button>
              <ListDownloads
                listId={list.id}
                name={list.name}
                disabled={!list.count || syncing}
              />
            </>
          )}
          <Link
            className="shelf-action"
            to={`/discover?view=yours&list=${list.id}`}
          >
            View all
          </Link>
          <ShelfPagination
            page={1}
            hasMore={books.hasNextPage}
            max={Math.ceil((books.data?.count ?? list.count) / 16)}
            busy={books.isFetching}
            onPage={() => {}}
            infinite={{
              fetchNextPage: books.fetchNextPage,
              isFetchNextPageError: books.isFetchNextPageError,
              count: books.data?.items.length || 0,
            }}
            label={list.name}
          />
        </div>
      </div>
      <Notice
        error={books.error || pin.error || refresh.error || subscription.error}
      />
      {syncing && (
        <p className="explore-footnote" role="status">
          Refreshing list…
        </p>
      )}
      {subscription.data?.state === "failed" && (
        <p role="status">{subscription.data.message}</p>
      )}
      {items.length ? (
        <ul
          ref={shelf}
          className="discovery-shelf"
          aria-label={`${list.name} book preview`}
        >
          {items.map((work) => (
            <li key={work.id}>
              <EnrichedBookCard work={work} />
            </li>
          ))}
          {!books.hasNextPage && (
            <ShelfViewAll to={`/discover?view=yours&list=${list.id}`} />
          )}
        </ul>
      ) : (
        <p className="explore-footnote">
          {emptyShelfCopy(
            provider,
            books.data?.count ?? list.count,
            Boolean(subscription.data?.last_success_at) &&
              subscription.data?.state === "idle",
            subscription.data?.state === "failed",
          )}{" "}
          {canEdit && (
            <Link to={`/settings?list=${list.id}#reading`}>
              List settings →
            </Link>
          )}
        </p>
      )}
    </section>
  );
}

export function PersonalListPage({
  id,
  canEdit,
}: {
  id: string;
  canEdit: boolean;
}) {
  const query = usePagedQuery({
    queryKey: ["discovery-personal", id, "all"],
    initial: 0,
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/lists/{list_id}", {
          signal,
          params: {
            path: { list_id: id },
            query: { limit: 40, offset, sort: "newest" },
          },
        }),
      ),
    next: (last, pages) => {
      const loaded = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && loaded < last.count ? loaded : undefined;
    },
  });
  return (
    <>
      <Link className="back-link" to="/discover?view=yours">
        ← Your lists
      </Link>
      <Notice error={query.error} />
      {query.isPending && <Loading />}
      {query.data && (
        <>
          <header className="explore-detail-heading">
            <div>
              <h2>{query.data.name}</h2>
              <p className="muted">{query.data.count.toLocaleString()} books</p>
            </div>
            {canEdit && query.data.editable && (
              <ListDownloads
                listId={id}
                name={query.data.name}
                disabled={!query.data.count}
              />
            )}
          </header>
          <ul className="explore-books" aria-label={query.data.name}>
            {query.data.items.map((work) => (
              <li key={work.id}>
                <EnrichedBookCard work={work} />
              </li>
            ))}
          </ul>
          <InfiniteScroll query={query} />
          {query.data.count === 0 && (
            <p className="explore-footnote">No books on this list.</p>
          )}
        </>
      )}
    </>
  );
}
