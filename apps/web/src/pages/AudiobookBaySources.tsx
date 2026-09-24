import SettingHelp from "../components/SettingHelp";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Empty, Notice } from "../components";
import ConnectionTestStatus, {
  SavedSecretIndicator,
} from "../components/ConnectionTestStatus";

type Connection = components["schemas"]["ABBConnectionView"];
type Search = components["schemas"]["ABBSearch"];

export default function AudiobookBaySources({
  admin,
  canAcquire,
}: {
  admin: boolean;
  canAcquire: boolean;
}) {
  const [params] = useSearchParams();
  const [q, setQ] = useState(params.get("q") || "");
  const [submitted, setSubmitted] = useState<Search | null>(null);
  const cache = useQueryClient();
  const navigate = useNavigate();

  const search = useMutation({
    mutationFn: async (body: Search) =>
      result(await api.POST("/api/sources/audiobookbay/search", { body })),
    onMutate: (body) => {
      setSubmitted(body);
      detail.reset();
    },
    onSettled: () => {
      if (admin) cache.invalidateQueries({ queryKey: ["abb-connection"] });
    },
  });
  const detail = useMutation({
    mutationFn: async (id: string) =>
      result(
        await api.GET("/api/sources/audiobookbay/results/{result_id}", {
          params: { path: { result_id: id } },
        }),
      ),
  });
  const inspect = useMutation({
    mutationFn: async (id: string) =>
      result(
        await api.POST(
          "/api/sources/audiobookbay/results/{result_id}/artifact",
          { params: { path: { result_id: id } } },
        ),
      ),
    onSuccess: (artifact) =>
      navigate(`/sources/artifacts/${artifact.id}?${params.toString()}`),
  });
  const busy = search.isPending || detail.isPending || inspect.isPending;
  return (
    <>
      <header className="page-heading">
        <div>
          <p className="eyebrow">DOWNLOAD SOURCES</p>
          <h1>Search AudiobookBay</h1>
          <p>
            Browse audiobook postings, compare narrators and inspect the torrent
            before requesting a download.
          </p>
          <div className="button-row">
            <Link to={`/sources?${params.toString()}`}>Search MAM</Link>
            <Link to={`/sources/prowlarr?${params.toString()}`}>
              Search Prowlarr
            </Link>
          </div>
        </div>
      </header>
      {admin && (
        <Link className="back-link" to="/settings#sources">
          Source settings →
        </Link>
      )}
      <form
        className="panel editor"
        onSubmit={(event) => {
          event.preventDefault();
          inspect.reset();
          search.mutate({ q, page: 1 });
        }}
      >
        <label>
          Search title, author or series
          <input
            required
            maxLength={300}
            value={q}
            onChange={(event) => setQ(event.target.value)}
          />
        </label>
        <button className="primary" disabled={busy || !q.trim()}>
          {search.isPending ? "Searching AudiobookBay…" : "Search source"}
        </button>
      </form>
      <Notice error={search.error || detail.error || inspect.error} />
      {inspect.isPending && (
        <p role="status">
          Resolving torrent metadata through qBittorrent. No payload download is
          started by inspection.
        </p>
      )}
      {search.data && !search.isPending && !search.isError && (
        <section aria-label="AudiobookBay results">
          <p role="status">
            {search.data.items.length} postings · Page {search.data.page}
          </p>
          {!search.data.items.length && (
            <Empty title="No matching postings">
              Try another title, author or series.
            </Empty>
          )}
          {search.data.items.map((item) => (
            <article
              key={item.id}
              className="panel editor source-release"
              aria-label={item.release.title}
            >
              <h2>{item.release.title}</h2>
              <p>
                {(item.release.authors || []).join(", ") ||
                  "Author not established"}
              </p>
              <p>
                Narrator:{" "}
                {(item.release.narrators || []).join(", ") || "Unknown"} ·{" "}
                {(item.release.formats || []).join(", ").toUpperCase() ||
                  "Format unknown"}{" "}
                ·{" "}
                {item.release.size_bytes == null
                  ? "Size unknown"
                  : `${item.release.size_bytes.toLocaleString()} bytes`}
              </p>
              <p className="muted">
                Seed count unknown · Raw posting title: {item.release.raw_title}
              </p>
              <div className="button-row">
                <button disabled={busy} onClick={() => detail.mutate(item.id)}>
                  {detail.isPending && detail.variables === item.id
                    ? "Loading posting details…"
                    : "Posting details"}
                </button>
                {canAcquire && (
                  <button
                    disabled={busy}
                    onClick={() => inspect.mutate(item.id)}
                  >
                    Inspect torrent
                  </button>
                )}
              </div>
              {detail.data &&
                detail.variables === item.id &&
                !detail.isPending &&
                !detail.isError && (
                  <div>
                    <p className="source-description">
                      {detail.data.description || "No posting description"}
                    </p>
                    {!!(detail.data.files || []).length && (
                      <details>
                        <summary>
                          Claimed files ({(detail.data.files || []).length})
                        </summary>
                        <p>
                          These are posting claims. Resolved torrent metadata
                          and downloaded files are checked separately.
                        </p>
                        <ul className="artifact-files">
                          {(detail.data.files || [])
                            .slice(0, 50)
                            .map((file, index) => (
                              <li key={index}>
                                <span className="break-text">{file.path}</span>
                                <span>
                                  {file.size_bytes == null
                                    ? "Unknown size"
                                    : `${file.size_bytes.toLocaleString()} B`}
                                </span>
                              </li>
                            ))}
                        </ul>
                        {(detail.data.files || []).length > 50 && (
                          <p>
                            Showing the first 50 claims. Inspect the torrent for
                            its verified manifest.
                          </p>
                        )}
                      </details>
                    )}
                    {detail.data.limitation && (
                      <p className="notice">{detail.data.limitation}</p>
                    )}
                  </div>
                )}
            </article>
          ))}
          <div className="button-row">
            <button
              disabled={busy || search.data.page <= 1}
              onClick={() =>
                submitted &&
                search.mutate({ ...submitted, page: search.data!.page - 1 })
              }
            >
              Previous page
            </button>
            <button
              disabled={
                busy || !search.data.has_more || search.data.page >= 201
              }
              onClick={() =>
                submitted &&
                search.mutate({ ...submitted, page: search.data!.page + 1 })
              }
            >
              Next page
            </button>
          </div>
        </section>
      )}
    </>
  );
}

export function AudiobookBayConnectionForm({ value }: { value: Connection }) {
  const cache = useQueryClient();
  const [url, setUrl] = useState(value.base_url);
  const [proxy, setProxy] = useState(value.proxy_url || "");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [clear, setClear] = useState(false);
  const [enabled, setEnabled] = useState(value.enabled || !value.configured);
  const [downloader, setDownloader] = useState(
    value.metadata_downloader_id || "",
  );
  const clients = useQuery({
    queryKey: ["downloaders"],
    queryFn: async () => result(await api.GET("/api/downloaders")),
  });
  const refresh = () =>
    cache.invalidateQueries({ queryKey: ["abb-connection"] });
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/sources/audiobookbay/connection", {
          body: {
            base_url: url,
            proxy_url: proxy || null,
            proxy_username: username || null,
            proxy_password: password || null,
            clear_proxy_credentials: clear,
            metadata_downloader_id: downloader || null,
            enabled,
            expected_generation: value.generation,
          },
        }),
      ),
    onSuccess: (connection) => {
      setUsername("");
      setPassword("");
      cache.setQueryData(["abb-connection"], connection);
      refresh();
    },
  });
  const test = useMutation({
    mutationFn: async () =>
      result(await api.POST("/api/sources/audiobookbay/connection/test")),
    onSuccess: (connection) =>
      cache.setQueryData(["abb-connection"], connection),
    onSettled: refresh,
  });
  return (
    <form
      className="panel editor"
      aria-label="AudiobookBay connection settings"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <label>
        Site origin
        <input
          type="url"
          required
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://your-supported-host"
          maxLength={2000}
        />
      </label>
      <div>
        <label htmlFor="abb-metadata-downloader">
          <span className="setting-subheading">
            Metadata downloader
            <SettingHelp label="connection options">
              Torrent inspection requires qBittorrent with metadata APIs (5.2+).
              Automatic requests use their selected downloader.{" "}
              <Link to="/settings#downloaders">Manage downloaders</Link>
            </SettingHelp>
          </span>
        </label>
        <select
          id="abb-metadata-downloader"
          value={downloader}
          onChange={(event) => setDownloader(event.target.value)}
        >
          <option value="">Browsing only — no default resolver</option>
          {clients.data
            ?.filter(
              (client) => client.enabled && client.kind === "qbittorrent",
            )
            .map((client) => (
              <option key={client.id} value={client.id}>
                {client.name}
              </option>
            ))}
        </select>
      </div>

      <details>
        <summary>Proxy routing</summary>
        <label>
          <span className="setting-subheading">
            HTTP proxy URL (optional)
            <SettingHelp label="connection options">
              {value.has_proxy_credentials
                ? "Credentials saved; blank fields preserve them on the same proxy."
                : "No proxy credentials saved."}{" "}
              When configured, all site requests use this proxy. qBittorrent's
              network route is configured separately.
            </SettingHelp>
          </span>
          <input
            type="url"
            value={proxy}
            onChange={(event) => setProxy(event.target.value)}
            maxLength={2000}
          />
        </label>
        <label>
          Proxy username
          <input
            autoComplete="off"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            maxLength={300}
          />
        </label>
        <label>
          <span className="credential-label">
            <span>Proxy password</span>
            <SavedSecretIndicator saved={value.has_proxy_credentials} />
          </span>
          <input
            aria-label="Proxy password"
            type="password"
            autoComplete="new-password"
            placeholder={
              value.has_proxy_credentials &&
              !clear &&
              proxy === (value.proxy_url || "")
                ? "••••••••"
                : undefined
            }
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            maxLength={1000}
          />
        </label>

        <label className="check-label">
          <input
            type="checkbox"
            checked={clear}
            onChange={(event) => setClear(event.target.checked)}
          />
          Clear saved proxy credentials
        </label>
      </details>
      <label className="check-label">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
        />
        Enable AudiobookBay
      </label>
      <div className="connection-action-bar">
        <div className="button-row">
          <button
            className="primary"
            disabled={save.isPending || test.isPending}
          >
            {save.isPending ? "Saving…" : "Save connection"}
          </button>
          <button
            type="button"
            disabled={save.isPending || test.isPending || !value.enabled}
            onClick={() => test.mutate()}
          >
            {test.isPending ? "Testing…" : "Test connection"}
          </button>
        </div>
        <ConnectionTestStatus
          configured={value.configured}
          status={value.status}
          lastSuccessAt={value.last_success_at}
          isPending={test.isPending}
          error={test.error}
        />
      </div>
      <Notice error={save.error || test.error || clients.error} />
      {value.last_error && !test.error && (
        <p className="notice">{value.last_error}</p>
      )}
    </form>
  );
}
