import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "../components/InfiniteScroll";
import { HardcoverCollections } from "./CommunityLists";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowLeft,
  Check,
  Search,
  Settings,
  SlidersHorizontal,
  Trophy,
} from "lucide-react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
import CustomizeDiscover, {
  type DiscoverLayout,
} from "../components/CustomizeDiscover";
import { useDiscoverShelfSources } from "../hooks/useDiscoverShelfSources";
import FollowedLists, {
  PersonalListPage,
  PersonalRow,
} from "../components/FollowedLists";
import DiscoveryShelf from "../components/DiscoveryShelf";
import ListDownloads from "../components/ListDownloads";
import ShelfPagination from "../components/ShelfPagination";
import SeriesContinuation from "../components/SeriesContinuation";
import RecentLibrary from "../components/RecentLibrary";
import { useTrackStoryGraphToRead } from "../hooks/useTrackStoryGraphToRead";
import {
  CollectionActions,
  CollectionBooks,
  CollectionRow,
  CollectionTile,
  genreLabel,
  useCollections,
} from "../components/DiscoveryCollections";
import ReleaseCalendar, { UpcomingShelf } from "./ReleaseCalendar";

const views = [
  ["home", "For you"],
  ["browse", "Browse"],
  ["calendar", "Calendar"],
  ["collections", "Collections"],
  ["awards", "Awards"],
  ["yours", "Your lists"],
] as const;
export default function Discover({ canEdit = true }: { canEdit?: boolean }) {
  const [params] = useSearchParams();
  const { collectionId } = useParams();
  const view = params.get("view") || "home";
  const personal = params.get("list");
  const shelf = params.get("shelf");
  return (
    <div className="explore">
      <h1 className="sr-only">Discover</h1>
      {(collectionId || view !== "home") && (
        <DiscoverNavigation view={view} collectionId={collectionId} />
      )}
      {personal ? (
        <PersonalListPage key={personal} id={personal} canEdit={canEdit} />
      ) : shelf === "trending" || shelf === "new-releases" ? (
        <ProviderShelf key={shelf} shelf={shelf} canEdit={canEdit} full />
      ) : collectionId ? (
        <CollectionPage
          key={collectionId}
          id={collectionId}
          canEdit={canEdit}
        />
      ) : view === "home" ? (
        <Home canEdit={canEdit} />
      ) : view === "browse" ? (
        <Browse />
      ) : view === "calendar" ? (
        <ReleaseCalendar canEdit={canEdit} />
      ) : view === "yours" ? (
        <YourLists canEdit={canEdit} />
      ) : (
        <CollectionIndex awards={view === "awards"} />
      )}
    </div>
  );
}

function DiscoverNavigation({
  view = "home",
  collectionId,
  children,
}: {
  view?: string;
  collectionId?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="page-tabs-bar">
      <nav className="page-tabs" aria-label="Discover navigation">
        {views.map(([key, label]) => (
          <Link
            key={key}
            to={key === "home" ? "/discover" : `/discover?view=${key}`}
            aria-current={!collectionId && view === key ? "page" : undefined}
          >
            {label}
          </Link>
        ))}
      </nav>
      {children && <div className="page-tabs-tools">{children}</div>}
    </div>
  );
}

function Home({ canEdit }: { canEdit: boolean }) {
  const cache = useQueryClient();
  const account = useQuery({
    queryKey: ["metadata-account"],
    queryFn: async () => result(await api.GET("/api/metadata/account")),
  });
  const index = useCollections({ limit: 100 });
  const publicLists = useCollections({ kind: "listopia", limit: 100 });
  const saved = useCollections({ saved: true, limit: 100 });
  const layout = useQuery({
    queryKey: ["discovery-layout"],
    queryFn: async () => result(await api.GET("/api/discovery/layout")),
  });
  const [customizing, setCustomizing] = useState(false);
  const sources = useDiscoverShelfSources();
  const save = useMutation({
    mutationFn: async (draft: DiscoverLayout) => {
      for (const c of sources.collections.data || []) {
        if (
          canEdit &&
          draft.order.includes(c.id) &&
          !rows.some((r) => r.id === c.id)
        ) {
          result(
            await api.PUT("/api/discovery/collections/{collection_id}/follow", {
              params: { path: { collection_id: c.id } },
              body: { pinned: true, tracking: c.tracking },
            }),
          );
        }
      }
      return result(await api.PUT("/api/discovery/layout", { body: draft }));
    },
    onSuccess: (value) => {
      cache.setQueryData(["discovery-layout"], value);
      void cache.invalidateQueries({ queryKey: ["discovery-collections"] });
      void cache.invalidateQueries({
        queryKey: ["discover-shelf-collections"],
      });
      setCustomizing(false);
    },
  });
  const award = index.data?.items.find(
    (c) => c.year === index.data?.years[0] && c.category === "Fiction",
  );
  const fantasy = index.data?.items.find(
    (c) => c.year === index.data?.years[0] && c.category === "Fantasy",
  );
  const rows: { id: string; title: string; content: React.ReactNode }[] = [];
  if (account.data?.enabled)
    rows.push({
      id: "trending",
      title: "Trending on Hardcover",
      content: <ProviderShelf canEdit={canEdit} shelf="trending" />,
    });
  if (award && !saved.data?.items.some((c) => c.id === award.id && !c.pinned))
    rows.push({
      id: award.id,
      title: `${award.year} Fiction nominees`,
      content: <CollectionRow canEdit={canEdit} collection={award} />,
    });
  for (const list of sources.lists.data || [])
    rows.push({
      id: `personal:${list.id}`,
      title: list.name,
      content: (
        <PersonalRow
          list={list}
          manage={false}
          canEdit={canEdit && list.editable}
        />
      ),
    });
  for (const c of saved.data?.items.filter((c) => c.pinned) || [])
    if (!rows.some((r) => r.id === c.id))
      rows.push({
        id: c.id,
        title:
          c.kind === "award"
            ? `${c.year} ${c.category || c.title} nominees`
            : c.title,
        content: <CollectionRow canEdit={canEdit} collection={c} />,
      });
  if (
    fantasy &&
    !saved.data?.items.some((c) => c.id === fantasy.id && !c.pinned) &&
    !rows.some((r) => r.id === fantasy.id)
  )
    rows.push({
      id: fantasy.id,
      title: `${fantasy.year} Fantasy nominees`,
      content: <CollectionRow canEdit={canEdit} collection={fantasy} />,
    });
  for (const genre of ["science-fiction", "mystery"]) {
    const collection = publicLists.data?.items.find((c) =>
      c.genres.includes(genre),
    );
    if (
      collection &&
      !rows.some((r) => r.id === collection.id) &&
      !saved.data?.items.some((c) => c.id === collection.id && !c.pinned)
    )
      rows.push({
        id: collection.id,
        title: collection.title,
        content: <CollectionRow canEdit={canEdit} collection={collection} />,
      });
  }
  const seriesRow = {
    id: "series",
    title: "Missing from your series",
    content: <SeriesContinuation hideEmpty />,
  };
  if (account.data?.suggest_series_gaps) rows.push(seriesRow);
  rows.push({
    id: "library",
    title: "Recent library additions",
    content: <RecentLibrary hideEmpty />,
  });
  if (account.data?.enabled)
    rows.push({
      id: "new-releases",
      title: "New releases",
      content: <ProviderShelf canEdit={canEdit} shelf="new-releases" />,
    });
  if (account.data?.enabled)
    rows.push({
      id: "upcoming",
      title: "Upcoming releases",
      content: <UpcomingShelf canEdit={canEdit} />,
    });
  if (!account.data?.suggest_series_gaps) rows.push(seriesRow);
  for (const c of sources.collections.data || []) {
    if (layout.data?.order?.includes(c.id) && !rows.some((r) => r.id === c.id))
      rows.push({
        id: c.id,
        title:
          c.kind === "award"
            ? `${c.year} ${c.category || c.title} nominees`
            : c.title,
        content: <CollectionRow canEdit={canEdit} collection={c} />,
      });
  }
  const order = (layout.data?.order || []).flatMap((id) =>
    id === "personal"
      ? (sources.lists.data || []).map((l) => `personal:${l.id}`)
      : [id],
  );
  const hidden = [
    ...new Set([
      ...(layout.data?.hidden || []),
      ...(layout.data?.hidden?.includes("personal")
        ? (sources.lists.data || []).map((l) => `personal:${l.id}`)
        : []),
    ]),
  ];
  const options = [
    ...rows.map((r) => ({
      id: r.id,
      title: r.title,
      source: r.id.startsWith("personal:")
        ? "Your lists"
        : sources.collections.data?.some((c) => c.id === r.id)
          ? sources.collections.data.find((c) => c.id === r.id)?.kind ===
            "listopia"
            ? "Community lists"
            : "Master lists"
          : "Discover",
    })),
    ...(sources.collections.data || [])
      .filter((c) => !rows.some((r) => r.id === c.id))
      .map((c) => ({
        id: c.id,
        title:
          c.kind === "award"
            ? `${c.year} ${c.category || c.title} nominees`
            : c.title,
        source: c.kind === "listopia" ? "Community lists" : "Master lists",
      })),
  ];
  rows.sort((a, b) => {
    const ai = order.indexOf(a.id),
      bi = order.indexOf(b.id);
    return (ai < 0 ? 999 : ai) - (bi < 0 ? 999 : bi);
  });
  return (
    <>
      <DiscoverNavigation>
        <div className="explore-home-tools">
          <button
            disabled={
              sources.lists.isPending ||
              sources.collections.isPending ||
              !!sources.lists.error ||
              !!sources.collections.error ||
              layout.isPending ||
              index.isPending ||
              saved.isPending ||
              publicLists.isPending ||
              !!layout.error ||
              !!index.error ||
              !!saved.error ||
              !!publicLists.error
            }
            onClick={() => {
              save.reset();
              setCustomizing(true);
            }}
          >
            <SlidersHorizontal size={15} /> Customize
          </button>
        </div>
      </DiscoverNavigation>
      <Notice
        error={
          index.error ||
          saved.error ||
          layout.error ||
          account.error ||
          sources.lists.error ||
          sources.collections.error
        }
      />
      {(index.isPending || layout.isPending) && <Loading />}
      {!layout.isPending &&
        rows
          .filter((r) => !hidden.includes(r.id))
          .map((row) => <div key={row.id}>{row.content}</div>)}
      {rows.length > 0 && rows.every((r) => hidden.includes(r.id)) && (
        <p className="explore-empty">
          Your home is clear. Choose shelves in Customize or add a collection.
        </p>
      )}
      {customizing && (
        <CustomizeDiscover
          shelves={options}
          initial={{
            order: rows.map((r) => r.id),
            hidden: hidden.filter((id) => id !== "personal"),
          }}
          close={() => setCustomizing(false)}
          save={(draft) => save.mutate(draft)}
          busy={save.isPending}
          error={save.error}
        />
      )}
    </>
  );
}

function CollectionIndex({ awards }: { awards: boolean }) {
  const [params, setParams] = useSearchParams();
  const year = params.get("year") ? Number(params.get("year")) : undefined;
  const category = params.get("category") || "";
  const genre = params.get("genre") || "";
  const q = params.get("q") || "";
  const source = params.get("source") || "all";
  const query = usePagedQuery({
    queryKey: ["discovery-collections", { awards, year, category, genre, q }],
    queryFn: async (page, signal) =>
      result(
        await api.GET("/api/discovery/collections", {
          params: {
            query: {
              kind: awards ? "award" : "listopia",
              year,
              category,
              genre,
              q,
              page,
            },
          },
          signal,
        }),
      ),
    next: (last, pages) =>
      pages.reduce((n, p) => n + p.items.length, 0) < last.total
        ? pages.length + 1
        : undefined,
  });
  function change(key: string, value: string) {
    setParams((p) => {
      const next = new URLSearchParams(p);
      value ? next.set(key, value) : next.delete(key);
      next.delete("page");
      return next;
    });
  }
  return (
    <>
      <div className="explore-view-heading">
        <div>
          <h2>
            {awards ? "Goodreads Choice Awards" : "Collections worth exploring"}
          </h2>
          <p>
            {awards
              ? "Explore winners and nominees by year."
              : "Reader-curated lists, ready for your next chapter."}
          </p>
        </div>
      </div>
      <div className="explore-filters">
        {awards ? (
          <>
            <label>
              Year
              <select
                value={year || ""}
                onChange={(e) => change("year", e.target.value)}
              >
                <option value="">All years</option>
                {query.data?.years.map((y) => (
                  <option key={y}>{y}</option>
                ))}
              </select>
            </label>
            <label>
              Category
              <select
                value={category}
                onChange={(e) => change("category", e.target.value)}
              >
                <option value="">All categories</option>
                {query.data?.categories.map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </select>
            </label>
            <Link
              className="back-link"
              to={`/discover?view=browse&winners=1${year ? `&year=${year}` : ""}${category ? `&category=${encodeURIComponent(category)}` : ""}`}
            >
              Browse winners <Trophy size={14} />
            </Link>
          </>
        ) : (
          <label>
            Genre
            <select
              value={genre}
              onChange={(e) => change("genre", e.target.value)}
            >
              <option value="">All genres</option>
              {query.data?.genres.map((g) => (
                <option key={g} value={g}>
                  {genreLabel(g)}
                </option>
              ))}
            </select>
          </label>
        )}
        {!awards && (
          <label>
            Source
            <select
              aria-label="Source"
              value={source}
              onChange={(e) => change("source", e.target.value)}
            >
              <option value="all">All sources</option>
              <option value="goodreads">Goodreads</option>
              <option value="hardcover">Hardcover</option>
            </select>
          </label>
        )}
        <form
          className="explore-filter-search"
          onSubmit={(e) => {
            e.preventDefault();
            change("q", String(new FormData(e.currentTarget).get("q") || ""));
          }}
        >
          <input
            name="q"
            aria-label="Search collections"
            placeholder="Search collections"
            defaultValue={q}
            key={q}
          />
          <button aria-label="Search collections">
            <Search size={17} />
          </button>
        </form>
      </div>
      {(awards || source !== "hardcover") && (
        <>
          <Notice error={query.error} />
          {query.isPending && <Loading />}
          {query.data && (
            <>
              <div className="explore-collections">
                {query.data.items.map((c) => (
                  <CollectionTile collection={c} key={c.id} />
                ))}
              </div>
              {!query.data.items.length && (
                <p className="explore-empty">
                  No collections match. Try another filter.
                </p>
              )}
              <InfiniteScroll query={query} />
              {awards && query.data.archive_gaps.length > 0 && (
                <p className="explore-footnote">
                  Archive coverage is incomplete for{" "}
                  {query.data.archive_gaps.join(", ")}. Only verified results
                  are shown.
                </p>
              )}
            </>
          )}{" "}
        </>
      )}
      {!awards && source !== "goodreads" && (
        <HardcoverCollections
          term={[q, genreLabel(genre)].filter(Boolean).join(" ")}
        />
      )}
    </>
  );
}

function Browse() {
  const [params, setParams] = useSearchParams();
  const index = useCollections();
  const year = params.get("year") ? Number(params.get("year")) : undefined;
  const category = params.get("category") || "";
  const genre = params.get("genre") || "";
  const winners = params.get("winners") === "1";
  const owned = params.get("owned") === "1";
  const q = params.get("q") || "";
  const query = usePagedQuery({
    queryKey: [
      "discovery-browse",
      { year, category, genre, winners, owned, q },
    ],
    queryFn: async (page, signal) =>
      result(
        await api.GET("/api/discovery/browse", {
          params: { query: { page, year, category, genre, winners, owned, q } },
          signal,
        }),
      ),
    next: (last, pages) => (last.has_more ? pages.length + 1 : undefined),
    staleTime: 60_000,
  });
  function change(key: string, value: string) {
    setParams((p) => {
      const n = new URLSearchParams(p);
      value ? n.set(key, value) : n.delete(key);
      n.delete("page");
      return n;
    });
  }
  return (
    <>
      <div className="explore-view-heading">
        <div>
          <h2>
            {winners ? "The winners' shelf" : "Find something worth reading"}
          </h2>
          <p>Explore the books in our discovery collections.</p>
        </div>
      </div>
      <div className="explore-filters">
        <label>
          Genre
          <select
            value={genre}
            onChange={(e) => change("genre", e.target.value)}
          >
            <option value="">All genres</option>
            {index.data?.genres.map((g) => (
              <option key={g} value={g}>
                {genreLabel(g)}
              </option>
            ))}
          </select>
        </label>
        <label>
          Award year
          <select
            value={year || ""}
            onChange={(e) => change("year", e.target.value)}
          >
            <option value="">Any year</option>
            {index.data?.years.map((y) => (
              <option key={y}>{y}</option>
            ))}
          </select>
        </label>
        <button
          aria-pressed={winners}
          onClick={() => change("winners", winners ? "" : "1")}
        >
          <Trophy size={15} /> Winners
        </button>
        <button
          aria-pressed={owned}
          onClick={() => change("owned", owned ? "" : "1")}
        >
          <Check size={15} /> In library
        </button>
        <form
          className="explore-filter-search"
          onSubmit={(e) => {
            e.preventDefault();
            change("q", String(new FormData(e.currentTarget).get("q") || ""));
          }}
        >
          <input
            key={q}
            defaultValue={q}
            name="q"
            aria-label="Search discovery books"
            placeholder="Title or author"
          />
          <button aria-label="Search discovery books">
            <Search size={17} />
          </button>
        </form>
      </div>
      {category && (
        <button onClick={() => change("category", "")}>{category} ×</button>
      )}
      <Notice error={query.error} />
      {query.isPending && <Loading />}
      {query.data && (
        <>
          <p className="explore-result-count">
            {query.data.total.toLocaleString()} books
          </p>
          <CollectionBooks items={query.data.items} />
          {!query.data.items.length && (
            <p className="explore-empty">No books match these filters.</p>
          )}
          <InfiniteScroll query={query} />
        </>
      )}
    </>
  );
}

function CollectionPage({ id, canEdit }: { id: string; canEdit: boolean }) {
  const [winners, setWinners] = useState(false);
  const query = usePagedQuery({
    queryKey: ["discovery-collection", id, winners],
    queryFn: async (page, signal) =>
      result(
        await api.GET("/api/discovery/collections/{collection_id}", {
          params: {
            path: { collection_id: id },
            query: { page, winners, full: true },
          },
          signal,
        }),
      ),
    next: (last, pages) => (last.has_more ? pages.length + 1 : undefined),
    staleTime: 60_000,
  });
  const c = query.data?.collection;
  const items = [
    ...new Map(
      (query.data?.items || []).map((book) => [book.external_id, book]),
    ).values(),
  ];
  const total = query.loadedPages?.at(-1)?.total ?? c?.count ?? 0;
  return (
    <>
      <Link
        className="back-link explore-back"
        to={`/discover?view=${c?.kind === "award" ? "awards" : "collections"}`}
      >
        <ArrowLeft size={15} />
        {c?.kind === "award" ? "Awards" : "Collections"}
      </Link>
      <Notice error={query.error} />
      {query.isPending && <Loading />}
      {query.error && !query.data && (
        <button disabled={query.isFetching} onClick={() => query.refetch()}>
          Retry collection
        </button>
      )}
      {c && query.data && (
        <>
          <header className="explore-detail-heading">
            <div>
              <p className="explore-kicker">
                {c.kind === "award"
                  ? `${c.year} · Goodreads Choice Awards`
                  : "Goodreads · Listopia"}
              </p>
              <h2>{c.title}</h2>
              <p className="muted">
                {total.toLocaleString()}{" "}
                {c.kind === "award" ? "nominees" : "books"}
                {query.hasNextPage
                  ? ` · ${items.length.toLocaleString()} loaded`
                  : ""}
              </p>
            </div>
            <CollectionActions collection={c} canEdit={canEdit} />
          </header>
          {c.warning && <p className="notice">{c.warning}</p>}
          {c.kind === "award" && (
            <div className="explore-segment">
              <button
                aria-pressed={!winners}
                onClick={() => {
                  setWinners(false);
                }}
              >
                All nominees
              </button>
              <button
                aria-pressed={winners}
                onClick={() => {
                  setWinners(true);
                }}
              >
                <Trophy size={14} /> Winner
              </button>
            </div>
          )}
          <CollectionBooks items={items} />
          <InfiniteScroll query={query} />
          <p className="explore-footnote">
            {c.kind === "listopia"
              ? "Books load from Goodreads as you scroll."
              : `Updated ${new Date(c.updated_at).toLocaleDateString()} · Verified collection snapshot.`}
          </p>
        </>
      )}
    </>
  );
}

function YourLists({ canEdit }: { canEdit: boolean }) {
  const saved = useCollections({ saved: true, limit: 100 });
  const toRead = useTrackStoryGraphToRead(canEdit);
  return (
    <>
      <div className="explore-view-heading">
        <div>
          <h2>Your reading, your shelves</h2>
          <p>Connected shelves and collections you have saved.</p>
        </div>
        <Link
          className="list-settings-link"
          to="/settings#reading"
          aria-label="Reading accounts"
          title="Reading accounts settings"
        >
          <Settings size={20} aria-hidden="true" />
        </Link>
      </div>
      <FollowedLists home={false} canEdit={canEdit} />
      <Notice error={saved.error || toRead.error} />
      {saved.isPending && <Loading />}
      {!!saved.data?.items.length && (
        <>
          <h2 className="explore-subheading">Saved collections</h2>
          <div className="explore-collections">
            {saved.data.items.map((c) => (
              <CollectionTile key={c.id} collection={c} />
            ))}
          </div>
        </>
      )}
    </>
  );
}

function ProviderShelf({
  shelf,
  canEdit,
  full = false,
}: {
  shelf: "trending" | "new-releases";
  canEdit: boolean;
  full?: boolean;
}) {
  const query = usePagedQuery({
    queryKey: ["discovery", shelf, full],
    queryFn: async (page, signal) =>
      result(
        await api.GET("/api/discovery/hardcover/{shelf}", {
          signal,
          params: { path: { shelf }, query: { page } },
        }),
      ),
    staleTime: 300_000,
    retry: false,
    next: (last, pages) =>
      last.has_more &&
      pages.length < 25 &&
      (full || pages.reduce((n, p) => n + (p.items?.length || 0), 0) < 100)
        ? pages.length + 1
        : undefined,
  });
  return (
    <section
      className="discovery-section"
      aria-label={
        shelf === "trending" ? "Trending books" : "Recently published books"
      }
    >
      <Notice error={query.error} />
      {query.isPending && <Loading />}
      {query.data && (
        <DiscoveryShelf
          shelf={{
            ...query.data,
            items: full ? query.data.items : query.data.items?.slice(0, 100),
          }}
          grid={full}
          viewAll={
            !full && !query.hasNextPage
              ? `/discover?view=browse&shelf=${shelf}`
              : undefined
          }
          controls={
            <div className="button-row">
              {canEdit && (
                <ListDownloads
                  source="shelf"
                  listId={shelf}
                  name={query.data.title}
                  disabled={!query.data.items?.length}
                />
              )}
              {!full && (
                <Link
                  className="shelf-action"
                  to={`/discover?view=browse&shelf=${shelf}`}
                >
                  View all
                </Link>
              )}
              {!full && (
                <ShelfPagination
                  page={1}
                  hasMore={query.hasNextPage}
                  busy={query.isFetching}
                  onPage={() => {}}
                  infinite={{
                    fetchNextPage: query.fetchNextPage,
                    isFetchNextPageError: query.isFetchNextPageError,
                    count: query.data?.items?.length || 0,
                  }}
                  label="shelf"
                  max={25}
                />
              )}
            </div>
          }
        />
      )}
      {full && <InfiniteScroll query={query} />}
      {(query.error || query.data?.status === "unavailable") && (
        <button disabled={query.isFetching} onClick={() => query.refetch()}>
          Retry shelf
        </button>
      )}
    </section>
  );
}
