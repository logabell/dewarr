import { DeleteSourceConnection } from "../components/DeleteConfiguration";
import SettingHelp from "../components/SettingHelp";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Notice } from "../components";
import ConnectionTestStatus, {
  SavedSecretIndicator,
} from "../components/ConnectionTestStatus";

type Connection = components["schemas"]["ProwlarrConnectionView"];
type Page = components["schemas"]["ProwlarrPage"];
type Search = components["schemas"]["ProwlarrSearch"];
type Batch = { indexer: number; name: string; page?: Page; error?: Error };

export default function ProwlarrSources({
  admin,
  canAcquire,
}: {
  admin: boolean;
  canAcquire: boolean;
}) {
  const [params] = useSearchParams();
  const [q, setQ] = useState(params.get("q") || "");
  const [medium, setMedium] = useState<Search["medium"]>("all");
  const [omitted, setOmitted] = useState<number[]>([]);
  const [batches, setBatches] = useState<Batch[]>([]);
  const [submitted, setSubmitted] = useState({ q: "", medium });
  const navigate = useNavigate();
  const cache = useQueryClient();
  const connection = useQuery({
    queryKey: ["prowlarr-connection"],
    enabled: admin,
    queryFn: async () =>
      result(await api.GET("/api/sources/prowlarr/connection")),
  });
  const indexers = useQuery({
    queryKey: ["prowlarr-indexers"],
    enabled: !admin || !!connection.data?.enabled,
    refetchOnWindowFocus: false,
    retry: false,
    queryFn: async () =>
      result(await api.GET("/api/sources/prowlarr/indexers")),
  });
  const eligible = (indexers.data || []).filter(
    (i) => i.enabled && i.supports_search && !i.excluded,
  );
  const search = useMutation({
    mutationFn: async (more?: Batch) => {
      const query = more ? submitted : { q: q.trim(), medium };
      if (!more) {
        setBatches([]);
        setSubmitted(query);
      }
      const targets = more
        ? eligible.filter((i) => i.id === more.indexer)
        : eligible.filter((i) => !omitted.includes(i.id));
      // The server serializes the shared API-key budget; render each completed source immediately.
      for (const indexer of targets) {
        try {
          const page = result(
            await api.POST("/api/sources/prowlarr/search", {
              body: {
                ...query,
                indexer_id: indexer.id,
                offset: more?.page ? more.page.offset + more.page.limit : 0,
                limit: 50,
              },
            }),
          );
          setBatches((previous) => {
            const prior = more
              ? previous.find((b) => b.indexer === indexer.id)?.page?.items ||
                []
              : [];
            const items = [
              ...new Map(
                [...prior, ...page.items].map((item) => [
                  item.release.source_id,
                  item,
                ]),
              ).values(),
            ];
            return [
              ...previous.filter((b) => b.indexer !== indexer.id),
              {
                indexer: indexer.id,
                name: indexer.name,
                page: { ...page, items },
              },
            ];
          });
        } catch (error) {
          setBatches((previous) => [
            ...previous.filter((b) => b.indexer !== indexer.id),
            {
              ...(more || {}),
              indexer: indexer.id,
              name: indexer.name,
              error:
                error instanceof Error
                  ? error
                  : new Error("Source search failed"),
            },
          ]);
        }
      }
    },
    onSettled: () => {
      if (admin) cache.invalidateQueries({ queryKey: ["prowlarr-connection"] });
    },
  });
  const inspect = useMutation({
    mutationFn: async (id: string) =>
      result(
        await api.POST("/api/sources/prowlarr/results/{result_id}/artifact", {
          params: { path: { result_id: id } },
        }),
      ),
    onSuccess: (artifact) =>
      navigate(
        `/sources/artifacts/${artifact.id}${params.get("request") ? `?request=${encodeURIComponent(params.get("request")!)}&slot=${encodeURIComponent(params.get("slot") || artifact.release.medium || "either")}` : ""}`,
      ),
  });
  const busy = search.isPending || inspect.isPending;
  return (
    <>
      <header className="page-heading">
        <div>
          <p className="eyebrow">DOWNLOAD SOURCES</p>
          <h1>Search Prowlarr</h1>
          <Link to={`/sources/audiobookbay?${params.toString()}`}>
            Search AudiobookBay
          </Link>
          <p>Compare book releases across your connected indexers.</p>
          <Link to={`/sources?${params.toString()}`}>Native MAM search</Link>
        </div>
      </header>
      {admin && (
        <Link className="back-link" to="/settings#sources">
          Source settings →
        </Link>
      )}
      <Notice error={indexers.error} />
      <Notice error={inspect.error} />
      <form
        className="panel editor"
        onSubmit={(event) => {
          event.preventDefault();
          search.mutate(undefined);
        }}
      >
        <label>
          Title, author or series
          <input
            value={q}
            onChange={(event) => setQ(event.target.value)}
            required
            maxLength={300}
          />
        </label>
        <label>
          Media
          <select
            value={medium}
            onChange={(event) =>
              setMedium(event.target.value as Search["medium"])
            }
          >
            <option value="all">Ebooks and audiobooks</option>
            <option value="ebook">Ebooks</option>
            <option value="audio">Audiobooks</option>
          </select>
        </label>
        <fieldset disabled={busy}>
          <legend>Search indexers</legend>
          {(indexers.data || []).map((indexer) => (
            <label key={indexer.id} className="check-label">
              <input
                type="checkbox"
                checked={
                  eligible.some((i) => i.id === indexer.id) &&
                  !omitted.includes(indexer.id)
                }
                disabled={!eligible.some((i) => i.id === indexer.id)}
                onChange={(event) =>
                  setOmitted((ids) =>
                    event.target.checked
                      ? ids.filter((id) => id !== indexer.id)
                      : [...ids, indexer.id],
                  )
                }
              />
              {indexer.name} · {indexer.protocol}
              {indexer.excluded
                ? " · excluded or handled by native MAM"
                : !indexer.enabled || !indexer.supports_search
                  ? " · unavailable"
                  : ""}
            </label>
          ))}
        </fieldset>
        <button
          className="primary"
          disabled={
            busy || !q.trim() || !eligible.some((i) => !omitted.includes(i.id))
          }
        >
          {search.isPending ? "Searching sources…" : "Search sources"}
        </button>
      </form>
      <p className="muted">
        Catalog identity, narrator, language and file format stay unknown when
        an indexer does not supply them. Torrent files go to qBittorrent and NZB
        files go to SABnzbd or NZBGet. Magnet-only and direct links are not
        supported.
      </p>
      <div aria-live="polite">
        {batches.map((batch) => (
          <section
            className="panel"
            key={batch.indexer}
            aria-label={`${batch.name} results`}
          >
            <h2>{batch.name}</h2>
            <Notice error={batch.error || null} />
            {batch.page?.warnings.map((warning) => (
              <p key={warning} className="muted">
                {warning}
              </p>
            ))}
            {batch.page?.items.length === 0 && <p>No releases returned.</p>}
            {batch.page?.items.map((item) => (
              <article
                key={item.release.source_id}
                className="panel editor source-release"
              >
                <h3 className="break-text">{item.release.raw_title}</h3>
                <p>
                  {item.release.medium || "Unknown medium"} ·{" "}
                  {item.release.protocol} ·{" "}
                  {item.release.size_bytes == null
                    ? "Unknown size"
                    : `${(item.release.size_bytes / 1024 / 1024).toFixed(1)} MiB`}{" "}
                  ·{" "}
                  {item.release.seeders == null
                    ? "Unknown seeders"
                    : `${item.release.seeders} seeders`}
                </p>
                {item.release.limitation && (
                  <p className="muted">{item.release.limitation}</p>
                )}
                {canAcquire && (
                  <button
                    disabled={busy || !item.release.acquisition_supported}
                    onClick={() => inspect.mutate(item.id)}
                  >
                    {item.release.protocol === "nzb"
                      ? "Inspect NZB"
                      : "Inspect torrent"}
                  </button>
                )}
              </article>
            ))}
            {batch.page?.may_have_more &&
              indexers.data?.find((i) => i.id === batch.indexer)
                ?.supports_pagination && (
                <button disabled={busy} onClick={() => search.mutate(batch)}>
                  More from {batch.name}
                </button>
              )}
          </section>
        ))}
      </div>
    </>
  );
}

export function ProwlarrConnectionForm({
  connection,
  onSaved,
}: {
  connection: Connection;
  onSaved: () => void;
}) {
  const cache = useQueryClient();
  const [url, setUrl] = useState(connection.base_url);
  const [key, setKey] = useState("");
  const [enabled, setEnabled] = useState(
    connection.enabled || !connection.configured,
  );
  const [excluded, setExcluded] = useState(
    connection.excluded_indexers.join(", "),
  );
  const save = useMutation({
    mutationFn: async () => {
      const ids = excluded
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean)
        .map(Number);
      if (ids.some((id) => !Number.isSafeInteger(id) || id < 1))
        throw new Error("Enter comma-separated positive indexer IDs");
      return result(
        await api.PUT("/api/sources/prowlarr/connection", {
          body: {
            base_url: url,
            api_key: key || undefined,
            enabled,
            excluded_indexers: ids,
            expected_generation: connection.generation,
          },
        }),
      );
    },
    onSuccess: (value) => {
      setKey("");
      cache.setQueryData(["prowlarr-connection"], value);
      onSaved();
    },
  });
  const test = useMutation({
    mutationFn: async () =>
      result(await api.POST("/api/sources/prowlarr/connection/test")),
    onSuccess: (value) => {
      cache.setQueryData(["prowlarr-connection"], value);
      onSaved();
    },
  });
  return (
    <form
      className="panel editor"
      aria-label="Prowlarr connection settings"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <label>
        Server URL
        <input
          type="url"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="http://prowlarr:9696"
          required
        />
      </label>
      <label>
        <span className="credential-label">
          <span>API key</span>
          <SavedSecretIndicator saved={connection.has_api_key} />
        </span>
        <input
          aria-label="API key"
          type="password"
          autoComplete="new-password"
          value={key}
          onChange={(event) => setKey(event.target.value)}
          placeholder={connection.has_api_key ? "••••••••" : "Prowlarr API key"}
          required={!connection.has_api_key}
        />
      </label>
      <label className="check-label">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
        />
        Enable Prowlarr
      </label>
      <details>
        <summary>Advanced</summary>
        <label>
          <span className="setting-subheading">
            Excluded indexer IDs
            <SettingHelp label="connection options">
              Native MAM automatically excludes the corresponding Prowlarr
              indexer while connected. Tracker proxies are configured in
              Prowlarr.
            </SettingHelp>
          </span>
          <input
            value={excluded}
            onChange={(event) => setExcluded(event.target.value)}
          />
        </label>
      </details>
      <div className="connection-action-bar">
        <div className="button-row">
          {connection.configured && (
            <DeleteSourceConnection
              source="prowlarr"
              name="Prowlarr"
              generation={connection.generation}
              disabled={save.isPending || test.isPending}
            />
          )}

          <button disabled={save.isPending || test.isPending}>
            {save.isPending ? "Saving…" : "Save connection"}
          </button>
          <button
            type="button"
            disabled={
              !connection.configured ||
              !connection.enabled ||
              save.isPending ||
              test.isPending
            }
            onClick={() => test.mutate()}
          >
            {test.isPending ? "Testing…" : "Test connection"}
          </button>
        </div>
        <ConnectionTestStatus
          configured={connection.configured}
          status={connection.status}
          lastSuccessAt={connection.last_success_at}
          isPending={test.isPending}
          error={test.error}
        />
      </div>
      <Notice error={save.error || test.error} />
      {connection.last_error && !test.error && (
        <p className="notice error">{connection.last_error}</p>
      )}
    </form>
  );
}
