import { coverImage, coverImageSet } from "../coverImages";
import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "../components/InfiniteScroll";
import ListDownloads from "../components/ListDownloads";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, Check, Plus } from "lucide-react";
import { Link, Navigate, useParams, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import DiscoveryShelf from "../components/DiscoveryShelf";
import { randomUUID } from "../randomUUID";

type Card = components["schemas"]["CommunityListCard"];

export default function CommunityLists({ canEdit }: { canEdit: boolean }) {
  const { externalId } = useParams();
  return externalId ? (
    <ListPreview key={externalId} externalId={externalId} canEdit={canEdit} />
  ) : (
    <CommunityIndex />
  );
}

function Covers({ list }: { list: Card }) {
  return (
    <div className="community-covers" aria-hidden="true">
      {list.covers.length ? (
        list.covers.map((cover, index) => (
          <img
            key={`${cover}:${index}`}
            src={coverImage(cover)}
            srcSet={coverImageSet(cover)}
            decoding="async"
            alt=""
            loading="lazy"
            referrerPolicy="no-referrer"
          />
        ))
      ) : (
        <BookOpen size={36} />
      )}
    </div>
  );
}

function CommunityIndex() {
  const [params] = useSearchParams();
  const next = new URLSearchParams(params);
  next.set("view", "collections");
  next.set("source", "hardcover");
  next.delete("page");
  return <Navigate replace to={`/discover?${next}`} />;
}

function ListPreview({
  externalId,
  canEdit,
}: {
  externalId: string;
  canEdit: boolean;
}) {
  const [key] = useState(() => randomUUID());
  const client = useQueryClient();
  const query = usePagedQuery({
    queryKey: ["community-lists", externalId],
    initial: 0,
    queryFn: async (cursor, signal) =>
      result(
        await api.GET("/api/discovery/lists/{external_id}", {
          params: { path: { external_id: externalId }, query: { cursor } },
          signal,
        }),
      ),
    next: (last) => last.next_cursor ?? undefined,
    retry: false,
  });
  const data = query.data;
  const layout = useQuery({
    queryKey: ["discovery-layout"],
    queryFn: async () => result(await api.GET("/api/discovery/layout")),
  });
  const follow = useMutation({
    mutationFn: async () => {
      const listId =
        data?.info.followed_list_id ??
        result(
          await api.POST("/api/discovery/lists/{external_id}/follow", {
            params: {
              path: { external_id: externalId },
              header: { "idempotency-key": key },
            },
            body: {},
          }),
        ).list_id;
      const latest = result(await api.GET("/api/discovery/layout"));
      const shelfKey = `personal:${listId}`;
      const updated = latest.hidden?.includes(shelfKey)
        ? result(
            await api.PUT("/api/discovery/layout", {
              body: {
                order: latest.order,
                hidden: (latest.hidden ?? []).filter((id) => id !== shelfKey),
              },
            }),
          )
        : latest;
      client.setQueryData(["discovery-layout"], updated);
      return listId;
    },
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["lists"] });
      client.invalidateQueries({ queryKey: ["community-lists"] });
      client.invalidateQueries({ queryKey: ["discovery-personal"] });
    },
  });
  const followedId = follow.data ?? data?.info.followed_list_id;
  const pinned =
    !!followedId &&
    !!layout.data &&
    !layout.data.hidden?.includes(`personal:${followedId}`);
  return (
    <>
      <Link className="back-link" to="/discover/lists">
        ← Community lists
      </Link>
      <Notice error={query.error} />
      {query.isPending && <Loading />}
      {query.error && (
        <button onClick={() => query.refetch()} disabled={query.isFetching}>
          Retry preview
        </button>
      )}
      {data && (
        <>
          <header className="community-detail-heading">
            <p className="eyebrow">PUBLIC LIST · HARDCOVER</p>
            <h1>{data.info.name}</h1>
            <p className="muted">
              {data.info.count.toLocaleString()} books
              {data.info.followers != null
                ? ` · ${data.info.followers.toLocaleString()} followers`
                : ""}
            </p>
            {data.info.description && (
              <p className="community-detail-description">
                {data.info.description}
              </p>
            )}
            <div className="button-row community-detail-actions">
              {pinned ? (
                <Link className="shelf-action" to="/discover">
                  <Check size={16} aria-hidden="true" /> On For You
                </Link>
              ) : canEdit ? (
                <button
                  className="primary"
                  onClick={() => follow.mutate()}
                  disabled={
                    follow.isPending ||
                    layout.isPending ||
                    !!layout.error ||
                    (!followedId && !data.info.follow_supported)
                  }
                >
                  <Plus size={16} aria-hidden="true" />
                  {follow.isPending ? "Adding…" : "Add to For You"}
                </button>
              ) : null}
              {canEdit && (
                <ListDownloads
                  listId={externalId}
                  source="hardcover"
                  name={data.info.name}
                  disabled={follow.isPending || !data.info.count}
                />
              )}
            </div>
            {!canEdit && !followedId && (
              <p className="muted">
                A member account is required to add lists.
              </p>
            )}
            {!data.info.follow_supported && !followedId && (
              <p className="notice">
                This list exceeds the supported 5,000-book limit. You can still
                browse its books.
              </p>
            )}
            <Notice error={follow.error || layout.error} />
            {follow.isSuccess && (
              <p className="muted" role="status">
                Added to For You.
              </p>
            )}
          </header>
          <section
            className="discovery-section community-preview"
            aria-label="Books in this community list"
          >
            <DiscoveryShelf
              controls={<></>}
              shelf={{
                title: "Inside this list",
                attribution: "Included in this Hardcover community list",
                status: "ready",
                page: 1,
                stale: false,
                items: data.items,
                has_more: !!data.next_cursor,
                warning: data.warning,
              }}
            />
            <InfiniteScroll query={query} />
          </section>
        </>
      )}
    </>
  );
}

export function HardcoverCollections({ term }: { term: string }) {
  const account = useQuery({
    queryKey: ["metadata-account"],
    queryFn: async () => result(await api.GET("/api/metadata/account")),
  });
  const query = usePagedQuery({
    queryKey: ["community-lists", term],
    enabled: !!account.data?.enabled,
    queryFn: async (page, signal) =>
      result(
        await api.GET("/api/discovery/lists", {
          params: { query: { q: term, page } },
          signal,
        }),
      ),
    next: (last, pages) =>
      last.has_more && pages.length < 50 ? pages.length + 1 : undefined,
    retry: false,
  });
  return (
    <section aria-label="Hardcover collections">
      <Notice error={account.error || query.error} />
      {account.isPending || (account.data?.enabled && query.isPending) ? (
        <Loading />
      ) : null}
      {account.data && !account.data.enabled && (
        <p className="notice">
          <Link to="/settings#reading">Connect Hardcover</Link> to explore
          Hardcover collections.
        </p>
      )}
      {query.data?.warning && <p className="notice">{query.data.warning}</p>}
      <div className="explore-collections">
        {query.data?.items.map((list) => (
          <Link
            className="explore-collection"
            key={list.external_id}
            to={`/discover/lists/${list.external_id}`}
          >
            <Covers list={list} />
            <div className="explore-collection-meta">
              <span>Hardcover · Community</span>
            </div>
            <h3>{list.name}</h3>
            <p>{list.count.toLocaleString()} books</p>
          </Link>
        ))}
      </div>
      {query.data && !query.data.items.length && (
        <p className="explore-empty">
          No Hardcover collections match these filters.
        </p>
      )}
      <InfiniteScroll query={query} />
    </section>
  );
}
