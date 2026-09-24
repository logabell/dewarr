import InfiniteScroll from "../components/InfiniteScroll";
import SourceReleaseDownload from "../components/SourceReleaseDownload";
import { Info, Search as SearchIcon } from "lucide-react";
import SourceReleaseDetails, {
  ReleaseTags,
} from "../components/SourceReleaseDetails";
import { transferSize } from "./DownloadConstraints";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, result, type Auth } from "../api/client";
import { canStartDownload } from "../permissions";
import type { components } from "../api/schema";
import { Notice } from "../components";
import { randomUUID } from "../randomUUID";

const AutomaticSelection = lazy(() => import("./AutomaticSelection"));

function useCanDownloadRelease() {
  const { data: session } = useQuery<Auth | null>({
    queryKey: ["session"],
    enabled: false,
  });
  return (medium?: string | null) =>
    canStartDownload(
      session?.user.permissions,
      session?.user.role,
      medium === "ebook" || medium === "audio" ? medium : undefined,
    );
}

type Work = components["schemas"]["WorkView"];
type Search = components["schemas"]["BookSearchView"];

export default function BookSources({
  work,
  canAcquire,
}: {
  work: Work;
  canAcquire: boolean;
}) {
  const cache = useQueryClient();
  const canDownload = useCanDownloadRelease();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [q, setQ] = useState(work.title.slice(0, 300));
  const [medium, setMedium] = useState("all");

  const initial = useRef(false);
  const key = useRef(randomUUID());
  const requestId = params.get("request");
  const queryKey = ["book-sources", work.id, requestId];
  const request = useQuery({
    queryKey: ["source-request", requestId],
    enabled: !!requestId,
    queryFn: async () =>
      result(
        await api.GET("/api/requests/{intent_id}", {
          params: { path: { intent_id: requestId! } },
        }),
      ),
  });
  const profiles = useQuery({
    queryKey: ["release-profiles"],
    queryFn: async () => result(await api.GET("/api/acquisition/profiles")),
  });
  const search = useQuery({
    queryKey,
    queryFn: async () =>
      result(
        await api.GET("/api/catalog/works/{work_id}/source-searches/latest", {
          params: {
            path: { work_id: work.id },
            query: { request_id: requestId || undefined },
          },
        }),
      ),
    refetchInterval: (query) =>
      query.state.data?.status !== "completed"
        ? 1500
        : query.state.data.items.some(
              (item) =>
                item.download &&
                [
                  "preparing",
                  "queued",
                  "downloading",
                  "downloaded",
                  "needs-review",
                ].includes(item.download.state),
            )
          ? 3000
          : false,
  });
  const chosen =
    profiles.data?.find(
      (p) =>
        (p.id || "") ===
        (request.data?.release_policy
          ? request.data.release_policy.id || ""
          : (search.data?.profile.id ?? "")),
    ) || profiles.data?.[0];
  const begin = useMutation({
    mutationFn: async (offset: number) =>
      result(
        await api.POST("/api/catalog/works/{work_id}/source-searches", {
          params: {
            path: { work_id: work.id },
            header: { "idempotency-key": key.current },
          },
          body: {
            q,
            request_id: requestId,
            medium,
            offset,
            profile_id: chosen?.id,
            profile_generation: chosen?.generation,
            profile_effective_revision: chosen?.effective_revision,
          },
        }),
      ),
    onSuccess: (value) => {
      key.current = randomUUID();
      cache.setQueryData(queryKey, value);
    },
  });
  useEffect(() => {
    if (
      search.isSuccess &&
      search.data &&
      (!requestId || search.data.request_id === requestId) &&
      !initial.current
    ) {
      initial.current = true;
      setQ(search.data.query);
      setMedium(search.data.medium);
    }
    if (
      search.isSuccess &&
      (search.data === null ||
        (!!requestId && search.data?.request_id !== requestId)) &&
      (!requestId || request.isSuccess) &&
      profiles.isSuccess &&
      !initial.current
    ) {
      initial.current = true;
      begin.mutate(0);
    }
  }, [
    search.isSuccess,
    search.data,
    profiles.isSuccess,
    requestId,
    request.isSuccess,
    begin,
  ]);
  const inspect = useMutation({
    mutationFn: async (id: string) =>
      result(
        await api.POST(
          "/api/source-searches/{search_id}/results/{result_id}/artifact",
          { params: { path: { search_id: search.data!.id, result_id: id } } },
        ),
      ),
    onSuccess: (artifact) => {
      const context = new URLSearchParams({
        work: work.id,
        search: search.data!.id,
      });
      if (search.data?.profile.id) {
        context.set("profile", search.data.profile.id);
        context.set(
          "profile_generation",
          String(search.data.profile.generation),
        );
      }
      if (search.data?.profile.effective_revision)
        context.set(
          "profile_effective_revision",
          search.data.profile.base_effective_revision ||
            search.data.profile.effective_revision,
        );
      for (const field of ["request", "slot"])
        if (params.get(field)) context.set(field, params.get(field)!);
      navigate(`/sources/artifacts/${artifact.id}?${context}`);
    },
  });
  const busy =
    begin.isPending ||
    inspect.isPending ||
    (search.data && search.data.status !== "completed");
  const data =
    !requestId || search.data?.request_id === requestId ? search.data : null;
  return (
    <section className="book-sources" aria-label="Book download sources">
      <Notice
        error={
          search.error ||
          profiles.error ||
          request.error ||
          begin.error ||
          inspect.error
        }
      />
      <form
        className="source-search-toolbar compact-source-search"
        onSubmit={(event) => {
          event.preventDefault();
          key.current = randomUUID();
          begin.mutate(0);
        }}
      >
        <label>
          <span className="sr-only">Release search query</span>
          <input
            value={q}
            onChange={(event) => setQ(event.target.value)}
            required
            maxLength={300}
          />
        </label>
        <label>
          <span className="sr-only">Release medium</span>
          <select
            value={medium}
            onChange={(event) => setMedium(event.target.value)}
          >
            <option value="all">Ebook and audiobook</option>
            <option value="ebook">Ebook</option>
            <option value="audio">Audiobook</option>
          </select>
        </label>
        <button
          aria-label="Refresh source results"
          className="primary"
          disabled={
            !!busy ||
            !profiles.data ||
            (!!requestId && !request.isSuccess) ||
            !q.trim()
          }
        >
          <SearchIcon size={16} /> Search
        </button>
      </form>
      {data && (
        <Results
          key={data.id}
          data={data}
          inspecting={inspect.isPending}
          canAcquire={canAcquire}
          canDownload={canDownload}
          onInspect={(id) => inspect.mutate(id)}
        />
      )}
      {data &&
        canAcquire &&
        canDownload(params.get("slot") || undefined) &&
        params.get("request") &&
        ["ebook", "audio", "either"].includes(params.get("slot") || "") && (
          <Suspense fallback={<p>Loading release preparation…</p>}>
            <AutomaticSelection
              key={`${params.get("request")}:${params.get("slot")}`}
              search={data}
              requestId={params.get("request")!}
              slot={params.get("slot")!}
            />
          </Suspense>
        )}
      {data?.sources.some((source) => source.has_more) && (
        <button
          disabled={!!busy || data.offset >= 10000}
          onClick={() => {
            key.current = randomUUID();
            begin.mutate(data.offset + 50);
          }}
        >
          Next source page
        </button>
      )}
    </section>
  );
}

function Results({
  data,
  inspecting,
  canAcquire,
  canDownload,
  onInspect,
}: {
  data: Search;
  inspecting: boolean;
  canAcquire: boolean;
  canDownload: (medium?: string | null) => boolean;
  onInspect: (id: string) => void;
}) {
  const [pageIndex, setPageIndex] = useState(0);
  const [detailId, setDetailId] = useState<string | null>(null);
  const detailIndex = data.items.findIndex((item) => item.id === detailId);
  const detailItem = data.items[detailIndex];
  const [sort, setSort] = useState("profile");
  const [text, setText] = useState("");
  const [source, setSource] = useState("");
  const [format, setFormat] = useState("");
  const [hideBlocked, setHideBlocked] = useState(false);
  const resultsHeading = useRef<HTMLParagraphElement>(null);
  const origins = new Map(
    data.items.map(({ release }) => [sourceKey(release), sourceName(release)]),
  );
  const formats = [
    ...new Set(
      data.items.flatMap(({ release }) =>
        (release.formats || []).map((value) => value.toLowerCase()),
      ),
    ),
  ].sort();
  const needle = text.trim().toLowerCase();
  const filtered = data.items
    .map((item, rank) => ({ item, rank }))
    .filter(({ item }) => {
      const release = item.release;
      return (
        (!source || sourceKey(release) === source) &&
        (!format ||
          (format === "unknown"
            ? !release.formats?.length
            : release.formats?.some(
                (value) => value.toLowerCase() === format,
              ))) &&
        (!hideBlocked ||
          (item.current_connection && !item.assessment.blocked.length)) &&
        (!needle ||
          [
            release.title,
            release.raw_title,
            ...(release.authors || []),
            ...(release.narrators || []),
          ].some((value) => value.toLowerCase().includes(needle)))
      );
    });
  filtered.sort((a, b) => {
    let order = 0;
    if (sort === "seeds")
      order = compareKnown(
        a.item.release.seeders,
        b.item.release.seeders,
        true,
      );
    else if (sort === "smallest" || sort === "largest")
      order = compareKnown(
        a.item.release.size_bytes,
        b.item.release.size_bytes,
        sort === "largest",
      );
    else if (sort === "title")
      order = a.item.release.title.localeCompare(b.item.release.title);
    return order || a.rank - b.rank;
  });
  const filteredView = !!(needle || source || format || hideBlocked);
  const visibleCount = (pageIndex + 1) * 50;
  return (
    <>
      {data.status !== "completed" && (
        <p role="status" className="source-result-status">
          {data.message}
        </p>
      )}
      {data.sources
        .filter((source) => source.state === "failed")
        .map((source) => (
          <p className="notice error" key={source.key}>
            {source.name}: {source.message}
          </p>
        ))}
      {!data.sources.length && (
        <p className="notice">
          Connect a download source in Settings to find releases.
        </p>
      )}
      {data.status === "completed" && !data.items.length && (
        <p className="notice">No releases found. Try a different search.</p>
      )}
      {data.stale_identity && (
        <p className="notice error">
          The catalog identity or series evidence changed. Refresh the search
          before inspecting a result.
        </p>
      )}
      {data.items.length > 0 && (
        <section
          className="panel release-comparison"
          aria-label="Compare loaded releases"
        >
          <div className="library-filters">
            <label className="grow">
              <span className="sr-only">Filter title, author or narrator</span>
              <input
                placeholder="Filter releases…"
                value={text}
                maxLength={300}
                onChange={(event) => {
                  setText(event.target.value);
                  setPageIndex(0);
                }}
              />
            </label>
            <label>
              <span className="sr-only">Sort this view</span>
              <select
                value={sort}
                onChange={(event) => {
                  setSort(event.target.value);
                  setPageIndex(0);
                }}
              >
                <option value="profile">Preferred order</option>
                <option value="seeds">Most seeders</option>
                <option value="smallest">Smallest download</option>
                <option value="largest">Largest download</option>
                <option value="title">Title A–Z</option>
              </select>
            </label>

            <label>
              <span className="sr-only">Result source</span>
              <select
                value={source}
                onChange={(event) => {
                  setSource(event.target.value);
                  setPageIndex(0);
                }}
              >
                <option value="">All sources</option>
                {[...origins].map(([key, name]) => (
                  <option key={key} value={key}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span className="sr-only">Reported format</span>
              <select
                value={format}
                onChange={(event) => {
                  setFormat(event.target.value);
                  setPageIndex(0);
                }}
              >
                <option value="">All formats</option>
                {formats.map((value) => (
                  <option key={value} value={value}>
                    {value.toUpperCase()}
                  </option>
                ))}
                <option value="unknown">Unknown format</option>
              </select>
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={hideBlocked}
                onChange={(event) => {
                  setHideBlocked(event.target.checked);
                  setPageIndex(0);
                }}
              />
              Hide blocked or expired results
            </label>
          </div>

          {(filteredView || sort !== "profile") && (
            <button
              onClick={() => {
                setText("");
                setSource("");
                setFormat("");
                setHideBlocked(false);
                setSort("profile");
                setPageIndex(0);
              }}
            >
              Reset result view
            </button>
          )}
        </section>
      )}
      <p
        className="source-count"
        tabIndex={-1}
        ref={resultsHeading}
        aria-live="polite"
      >
        {data.items.length} distinct releases
        {filteredView ? ` · ${filtered.length} match your filters` : ""}
        {filtered.length > 0
          ? ` · showing ${1}–${Math.min(visibleCount, filtered.length)}`
          : ""}
      </p>
      {filteredView && !filtered.length && (
        <p className="notice">
          No loaded releases match these filters. Clear the filters to see the
          other results.
        </p>
      )}
      {!!filtered.length && (
        <div
          className="source-table-scroll"
          role="region"
          aria-label="Source releases"
          tabIndex={0}
        >
          <table className="source-table">
            <thead>
              <tr>
                <th scope="col">Title</th>
                <th scope="col">Author(s)</th>
                <th scope="col">Narrators</th>
                <th scope="col">Size</th>
                <th scope="col">Format</th>
                <th scope="col">Seeds</th>
                <th scope="col">Tags</th>
                <th scope="col">Status / actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.slice(0, visibleCount).map(({ item, rank }) => (
                <tr
                  className="source-release"
                  key={item.id}
                  aria-label={item.release.title}
                >
                  <td className="release-title-cell">
                    <button
                      className="release-title-button"
                      onClick={() => setDetailId(item.id)}
                    >
                      {item.release.title}
                    </button>
                    <small>
                      {sourceName(item.release)} · #{rank + 1}
                      {!item.current_connection
                        ? " · Expired"
                        : item.assessment.blocked.length
                          ? " · Blocked"
                          : ""}
                    </small>
                  </td>
                  <td>{item.release.authors?.join(", ") || "—"}</td>
                  <td>{item.release.narrators?.join(", ") || "—"}</td>
                  <td className="release-numeric">
                    {item.release.size_bytes == null
                      ? "Unknown"
                      : transferSize(item.release.size_bytes)}
                  </td>
                  <td>
                    <span className="release-format">
                      {item.release.formats?.join(", ").toUpperCase() ||
                        "Unknown"}
                    </span>
                    <small>
                      {item.release.medium === "audio"
                        ? "Audiobook"
                        : item.release.medium === "ebook"
                          ? "Ebook"
                          : "Unknown medium"}
                    </small>
                  </td>
                  <td className="release-numeric release-seeds">
                    {item.release.protocol === "soulseek"
                      ? "Peer online"
                      : (item.release.seeders?.toLocaleString() ?? "Unknown")}
                  </td>
                  <td>
                    <ReleaseTags release={item.release} />
                    {item.release.source === "mam" &&
                      !item.release.freeleech &&
                      !item.release.vip && <span className="muted">—</span>}
                  </td>
                  <td className="source-actions-cell">
                    <div className="source-row-actions">
                      <button
                        className="release-info-button"
                        aria-label={`Details for ${item.release.title}`}
                        title="Release details"
                        onClick={() => setDetailId(item.id)}
                      >
                        <Info size={18} />
                      </button>
                      {canAcquire && canDownload(item.release.medium) && (
                        <SourceReleaseDownload
                          searchId={data.id}
                          resultId={item.id}
                          title={item.release.title}
                          download={item.download}
                          offerWedge={
                            item.release.source === "mam" &&
                            !item.release.freeleech &&
                            !item.release.personal_freeleech
                          }
                          disabled={
                            inspecting ||
                            data.status !== "completed" ||
                            data.stale_identity ||
                            !item.current_connection ||
                            item.assessment.blocked.length > 0 ||
                            Date.parse(item.expires_at) <= Date.now()
                          }
                        />
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {detailItem && (
        <SourceReleaseDetails
          item={detailItem}
          rank={detailIndex}
          searchId={data.id}
          close={() => setDetailId(null)}
          canAcquire={canAcquire && canDownload(detailItem.release.medium)}
          onInspect={() => onInspect(detailItem.id)}
          disabled={
            inspecting ||
            data.stale_identity ||
            !detailItem.current_connection ||
            detailItem.assessment.blocked.length > 0
          }
        />
      )}
      <InfiniteScroll
        query={{
          hasNextPage: visibleCount < filtered.length,
          isFetching: inspecting,
          isFetchNextPageError: false,
          fetchNextPage: async () => setPageIndex((n) => n + 1),
        }}
      />
    </>
  );
}

function sourceKey(release: Search["items"][number]["release"]) {
  return release.source === "prowlarr"
    ? `prowlarr:${release.indexer_id}`
    : release.source;
}

function sourceName(release: Search["items"][number]["release"]) {
  return release.source === "mam"
    ? "MAM"
    : release.source === "audiobookbay"
      ? "AudiobookBay"
      : release.source === "slskd"
        ? "Soulseek"
        : `${release.indexer_name || "Prowlarr"} (indexer ${release.indexer_id})`;
}

function compareKnown(
  a: number | null | undefined,
  b: number | null | undefined,
  descending: boolean,
) {
  if (a == null) return b == null ? 0 : 1;
  if (b == null) return -1;
  return descending ? b - a : a - b;
}
