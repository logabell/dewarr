import { DeleteSourceConnection } from "../components/DeleteConfiguration";
import SettingHelp from "../components/SettingHelp";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Empty, Notice } from "../components";

type Query = components["schemas"]["MAMSearch"];
type Connection = components["schemas"]["MAMConnectionView"];
type Release = components["schemas"]["MAMRelease"];

export default function Sources({
  admin,
  canAcquire,
}: {
  admin: boolean;
  canAcquire: boolean;
}) {
  const [params] = useSearchParams();
  const [q, setQ] = useState(params.get("q") || "");
  const [medium, setMedium] = useState<Query["medium"]>("all");
  const [sort, setSort] = useState<Query["sort"]>("relevance");
  const [language, setLanguage] = useState("1");
  const [narrator, setNarrator] = useState(false);
  const [submitted, setSubmitted] = useState<Query | null>(null);
  const [detail, setDetail] = useState<Release | null>(null);
  const cache = useQueryClient();

  const search = useMutation({
    mutationFn: async (query: Query) =>
      result(await api.POST("/api/sources/mam/search", { body: query })),
    onMutate: (query) => {
      setSubmitted(query);
      setDetail(null);
    },
    onSettled: () => {
      if (admin) cache.invalidateQueries({ queryKey: ["mam-connection"] });
    },
  });
  const fetchDetail = useMutation({
    mutationFn: async (id: string) =>
      result(
        await api.GET("/api/sources/mam/releases/{source_id}", {
          params: { path: { source_id: id } },
        }),
      ),
    onSuccess: setDetail,
  });
  const busy = search.isPending || fetchDetail.isPending;
  return (
    <>
      <header className="page-heading">
        <div>
          <p className="eyebrow">DOWNLOAD SOURCES</p>
          <h1>Search MAM</h1>
          <Link to={`/sources/prowlarr?${params.toString()}`}>
            Search Prowlarr indexers
          </Link>
          <Link to={`/sources/audiobookbay?${params.toString()}`}>
            Search AudiobookBay
          </Link>
          <p>
            Find source releases by title, author or series, then inspect the
            edition and recording details.
          </p>
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
          search.mutate({
            q,
            medium,
            sort,
            language_ids: language.trim()
              ? language.split(",").map((item) => Number(item.trim()))
              : [],
            fields: narrator
              ? ["title", "author", "series", "narrator"]
              : ["title", "author", "series"],
            offset: 0,
            limit: 25,
          });
        }}
      >
        <label>
          Search title, author or series
          <input
            value={q}
            onChange={(event) => setQ(event.target.value)}
            maxLength={300}
            required
          />
        </label>
        <div className="form-grid">
          <label>
            Media
            <select
              value={medium}
              onChange={(event) =>
                setMedium(event.target.value as Query["medium"])
              }
            >
              <option value="all">Ebooks and audiobooks</option>
              <option value="ebook">Ebooks</option>
              <option value="audio">Audiobooks</option>
            </select>
          </label>
          <label>
            Source order
            <select
              value={sort}
              onChange={(event) => setSort(event.target.value as Query["sort"])}
            >
              <option value="relevance">MAM relevance</option>
              <option value="seeders">Most seeders</option>
            </select>
          </label>
        </div>
        <details>
          <summary>Search options</summary>
          <label className="check-label">
            <input
              type="checkbox"
              checked={narrator}
              onChange={(event) => setNarrator(event.target.checked)}
            />
            Include narrator names
          </label>
          <label>
            MAM language IDs
            <input
              value={language}
              onChange={(event) => setLanguage(event.target.value)}
              pattern="[0-9, ]*"
              maxLength={120}
            />
          </label>
          <p className="muted">
            1 is English. Separate IDs with commas; leave empty for all
            languages.
          </p>
        </details>
        <button className="primary" disabled={busy || !q.trim()}>
          {search.isPending ? "Searching MAM…" : "Search source"}
        </button>
      </form>
      <Notice error={search.error || fetchDetail.error} />
      {search.data && !search.isPending && !search.isError && (
        <section aria-label="MAM results">
          <p role="status">
            {search.data.items.length} releases on this page
            {search.data.total !== null
              ? ` · ${search.data.total} matching results`
              : " · total unknown"}
          </p>
          {search.data.warnings?.map((warning) => (
            <p className="notice" key={warning}>
              {warning}
            </p>
          ))}
          {!search.data.items.length && (
            <Empty title="No matching releases">
              Try another title, author or search option.
            </Empty>
          )}
          {search.data.items.map((release) => (
            <article
              className="panel editor source-release"
              key={release.source_id}
            >
              <h2>{release.title}</h2>
              <p>{release.authors?.join(", ") || "Author unknown"}</p>
              <p className="muted">
                {release.medium === "audio"
                  ? "Audiobook"
                  : release.medium === "ebook"
                    ? "Ebook"
                    : "Medium unknown"}{" "}
                · {release.filetype_display || "Format unknown"} ·{" "}
                {release.size_display || "Size unknown"}
              </p>
              <p>
                {release.narrators?.length
                  ? `Narrated by ${release.narrators?.join(", ")} · `
                  : ""}
                {release.seeders == null
                  ? "Seeds unknown"
                  : `${release.seeders} seeders`}{" "}
                ·{" "}
                {release.snatches == null
                  ? "Popularity unknown"
                  : `${release.snatches} snatches`}
              </p>
              <button
                disabled={busy}
                onClick={() => {
                  setDetail(release);
                  fetchDetail.mutate(release.source_id);
                }}
              >
                View source details
              </button>
              {detail?.source_id === release.source_id && (
                <ReleaseDetail
                  release={detail}
                  loading={fetchDetail.isPending}
                  canAcquire={canAcquire}
                />
              )}
            </article>
          ))}
          <div className="actions">
            <button
              disabled={busy || !submitted || !submitted.offset}
              onClick={() =>
                submitted &&
                search.mutate({
                  ...submitted,
                  offset: Math.max(0, (submitted.offset || 0) - 25),
                })
              }
            >
              Previous releases
            </button>
            <button
              disabled={busy || !submitted || !search.data.has_more}
              onClick={() =>
                submitted &&
                search.mutate({
                  ...submitted,
                  offset: (submitted.offset || 0) + 25,
                })
              }
            >
              More releases
            </button>
          </div>
        </section>
      )}
    </>
  );
}

function ReleaseDetail({
  release,
  loading,
  canAcquire,
}: {
  release: Release;
  loading: boolean;
  canAcquire: boolean;
}) {
  return (
    <section aria-label={`Details for ${release.title}`}>
      {loading && <p role="status">Refreshing source details…</p>}
      <dl className="source-facts">
        <dt>Raw title</dt>
        <dd>{release.raw_title}</dd>
        <dt>Source release</dt>
        <dd>MAM #{release.source_id}</dd>
        <dt>Category</dt>
        <dd>{release.category || "Unknown"}</dd>
        <dt>Language</dt>
        <dd>
          {release.language ||
            (release.language_id
              ? `MAM language ${release.language_id}`
              : "Unknown")}
        </dd>
        <dt>Series</dt>
        <dd>
          {release.series
            ?.map(
              (series) =>
                `${series.name}${series.position ? ` · ${series.position}` : ""}`,
            )
            .join("; ") || "Not supplied"}
        </dd>
        <dt>Tags</dt>
        <dd>{release.tags?.join(", ") || "Not supplied"}</dd>
        <dt>ISBN</dt>
        <dd>{release.isbn || "Not supplied"}</dd>
        <dt>Source flags</dt>
        <dd>
          Freeleech:{" "}
          {release.freeleech == null
            ? "unknown"
            : release.freeleech
              ? "yes"
              : "no"}{" "}
          · VIP: {release.vip == null ? "unknown" : release.vip ? "yes" : "no"}
        </dd>
        <dt>Uploaded</dt>
        <dd>{release.uploaded_at || "Unknown"}</dd>
      </dl>
      <p className="source-description">
        {release.description || "No source description supplied."}
      </p>
      {release.media_info && (
        <details>
          <summary>Media information</summary>
          <pre className="source-description">{release.media_info}</pre>
        </details>
      )}
      <p className="muted">
        Source metadata describes this release; it does not confirm a catalog
        edition, collection coverage or library ownership.
      </p>
      {canAcquire && !loading && (
        <TorrentInspection
          key={release.source_id}
          sourceId={release.source_id}
        />
      )}
    </section>
  );
}

function TorrentInspection({ sourceId }: { sourceId: string }) {
  const cache = useQueryClient();
  const [params] = useSearchParams();
  const context = new URLSearchParams();
  if (params.get("request")) context.set("request", params.get("request")!);
  if (params.get("slot")) context.set("slot", params.get("slot")!);
  const inspect = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/sources/mam/releases/{source_id}/artifact", {
          params: { path: { source_id: sourceId } },
        }),
      ),
    onSuccess: (artifact) =>
      cache.setQueryData(["source-artifact", artifact.id], artifact),
    onSettled: () => cache.invalidateQueries({ queryKey: ["mam-connection"] }),
  });
  return (
    <div>
      <button disabled={inspect.isPending} onClick={() => inspect.mutate()}>
        {inspect.isPending ? "Inspecting torrent…" : "Inspect torrent manifest"}
      </button>
      <Notice error={inspect.error} />
      {inspect.data && (
        <p role="status">
          {inspect.data.descriptor.files.length} file entries inspected.{" "}
          <Link to={`/sources/artifacts/${inspect.data.id}?${context}`}>
            View saved manifest
          </Link>
        </p>
      )}
      <p className="muted">
        Fetches the torrent metadata through your MAM connection. Does not start
        a download.
      </p>
    </div>
  );
}

type AutomationSettings = {
  seedbox_ip: boolean;
  seedbox_interval_seconds: number;
  auto_vip: boolean;
  vip_interval_hours: number;
  use_wedge: boolean;
  wedge_min_size: boolean;
  wedge_min_size_mb: number;
  protect_ratio: boolean;
  ratio_below: number;
  ratio_buy_gb: number;
  maintain_buffer: boolean;
  buffer_below_gb: number;
  buffer_buy_gb: number;
  spend_bonus: boolean;
  bonus_above: number;
  bonus_buy_gb: number;
  upload_interval_hours: number;
};

const AUTOMATION_DEFAULTS: AutomationSettings = {
  seedbox_ip: false,
  seedbox_interval_seconds: 300,
  auto_vip: false,
  vip_interval_hours: 24,
  use_wedge: false,
  wedge_min_size: false,
  wedge_min_size_mb: 0,
  protect_ratio: false,
  ratio_below: 2.5,
  ratio_buy_gb: 50,
  maintain_buffer: false,
  buffer_below_gb: 10,
  buffer_buy_gb: 50,
  spend_bonus: false,
  bonus_above: 5000,
  bonus_buy_gb: 50,
  upload_interval_hours: 3,
};

function automationSettings(connection: Connection): AutomationSettings {
  return { ...AUTOMATION_DEFAULTS, ...connection.automation };
}

function wholeNumber(value: string) {
  const next = Number.parseInt(value, 10);
  return Number.isInteger(next) ? next : null;
}

function boundedNumber(value: string, min: number, max: number) {
  const next = Number(value);
  return Number.isFinite(next) && next >= min && next <= max ? next : null;
}

function AccountAutomation({
  value,
  onChange,
}: {
  value: AutomationSettings;
  onChange: (value: AutomationSettings) => void;
}) {
  const enabledCount = [
    value.seedbox_ip,
    value.auto_vip,
    value.use_wedge,
    value.protect_ratio,
    value.maintain_buffer,
    value.spend_bonus,
  ].filter(Boolean).length;
  return (
    <details className="account-automation">
      <summary>
        <span>Account automation</span>
        <span className="account-automation-state">
          {enabledCount ? `${enabledCount} enabled` : "All off"}
        </span>
      </summary>
      <p className="muted">
        These stay off until you turn them on. They can spend bonus points, use
        a Freeleech wedge you already own, or change the IP MyAnonamouse treats
        as your seedbox.
      </p>
      <div className="helper-option">
        <div className="helper-toggle">
          <label className="check-label">
            <input
              type="checkbox"
              checked={value.seedbox_ip}
              onChange={(event) =>
                onChange({ ...value, seedbox_ip: event.target.checked })
              }
            />
            Auto-authorize seedbox IP
          </label>
          <SettingHelp label="seedbox IP">
            Checks the public IP of this server&apos;s route to MyAnonamouse on
            the interval below. When that IP or network changes, or the last
            update is a day old, Dewarr updates the dynamic seedbox so the
            tracker accepts the address. The IP check does not send your MAM
            cookie.
          </SettingHelp>
        </div>
        {value.seedbox_ip && (
          <label>
            Check interval (seconds)
            <input
              type="number"
              min={60}
              max={86400}
              value={value.seedbox_interval_seconds}
              onChange={(event) => {
                const next = wholeNumber(event.target.value);
                if (next !== null)
                  onChange({ ...value, seedbox_interval_seconds: next });
              }}
            />
          </label>
        )}
      </div>
      <div className="helper-option">
        <div className="helper-toggle">
          <label className="check-label">
            <input
              type="checkbox"
              checked={value.auto_vip}
              onChange={(event) =>
                onChange({ ...value, auto_vip: event.target.checked })
              }
            />
            Auto-max VIP
          </label>
          <SettingHelp label="VIP top-up">
            Spends bonus points to extend VIP, up to the longest purchase
            MyAnonamouse allows. Skips the purchase when you cannot afford one
            week or VIP is already at that cap.
          </SettingHelp>
        </div>
        {value.auto_vip && (
          <label>
            Top-up interval (hours)
            <input
              type="number"
              min={1}
              max={168}
              value={value.vip_interval_hours}
              onChange={(event) => {
                const next = wholeNumber(event.target.value);
                if (next !== null)
                  onChange({ ...value, vip_interval_hours: next });
              }}
            />
          </label>
        )}
      </div>
      <div className="helper-option">
        <div className="helper-toggle">
          <label className="check-label">
            <input
              type="checkbox"
              checked={value.use_wedge}
              onChange={(event) =>
                onChange({ ...value, use_wedge: event.target.checked })
              }
            />
            Use a Freeleech wedge on download
          </label>
          <SettingHelp label="Freeleech wedge">
            On each manual or automatic download, ask MyAnonamouse to spend one
            Freeleech wedge you already own when the torrent is not already
            free. This does not buy a wedge. A search result can also spend one
            wedge for that torrent alone. Public freeleech, a wedge already
            applied, and VIP freeleech while VIP is active are left alone. If
            MyAnonamouse refuses, the torrent is not sent to the download
            client.
          </SettingHelp>
        </div>
        {value.use_wedge && (
          <>
            <label className="check-label">
              <input
                type="checkbox"
                checked={value.wedge_min_size}
                onChange={(event) =>
                  onChange({ ...value, wedge_min_size: event.target.checked })
                }
              />
              Only for torrents larger than a minimum size
            </label>
            {value.wedge_min_size && (
              <label>
                Minimum size (MB)
                <input
                  type="number"
                  min={0}
                  step="0.1"
                  value={value.wedge_min_size_mb}
                  onChange={(event) => {
                    const next = boundedNumber(
                      event.target.value,
                      0,
                      10_000_000,
                    );
                    if (next !== null)
                      onChange({ ...value, wedge_min_size_mb: next });
                  }}
                />
              </label>
            )}
          </>
        )}
      </div>
      <div className="setting-subheading">
        <h3>Upload credit</h3>
        <SettingHelp label="upload credit">
          Buys the amount you set while the matching rule is on. Ratio is
          checked before the reserve. Extra bonus points can buy again in the
          same check until the balance falls to the threshold.
        </SettingHelp>
      </div>
      <div className="helper-option">
        <div className="helper-toggle">
          <label className="check-label">
            <input
              type="checkbox"
              checked={value.protect_ratio}
              onChange={(event) =>
                onChange({ ...value, protect_ratio: event.target.checked })
              }
            />
            Protect minimum ratio
          </label>
          <SettingHelp label="minimum ratio">
            Buys upload credit when your ratio falls below the number you set.
          </SettingHelp>
        </div>
        {value.protect_ratio && (
          <div className="automation-fields">
            <label>
              If ratio falls below
              <input
                type="number"
                min={0.1}
                max={1000}
                step="0.1"
                value={value.ratio_below}
                onChange={(event) => {
                  const next = boundedNumber(event.target.value, 0.1, 1000);
                  if (next !== null) onChange({ ...value, ratio_below: next });
                }}
              />
            </label>
            <label>
              Buy (GB)
              <input
                type="number"
                min={50}
                max={100000}
                value={value.ratio_buy_gb}
                onChange={(event) => {
                  const next = wholeNumber(event.target.value);
                  if (next !== null && next >= 50)
                    onChange({ ...value, ratio_buy_gb: next });
                }}
              />
            </label>
          </div>
        )}
      </div>
      <div className="helper-option">
        <div className="helper-toggle">
          <label className="check-label">
            <input
              type="checkbox"
              checked={value.maintain_buffer}
              onChange={(event) =>
                onChange({ ...value, maintain_buffer: event.target.checked })
              }
            />
            Maintain credit reserve
          </label>
          <SettingHelp label="credit reserve">
            Buys upload credit when uploaded minus downloaded falls below this
            many gigabytes. Skipped when the ratio rule already bought credit in
            the same check.
          </SettingHelp>
        </div>
        {value.maintain_buffer && (
          <div className="automation-fields">
            <label>
              If reserve falls below (GB)
              <input
                type="number"
                min={0}
                max={10000000}
                step="0.1"
                value={value.buffer_below_gb}
                onChange={(event) => {
                  const next = boundedNumber(event.target.value, 0, 10_000_000);
                  if (next !== null)
                    onChange({ ...value, buffer_below_gb: next });
                }}
              />
            </label>
            <label>
              Buy (GB)
              <input
                type="number"
                min={50}
                max={100000}
                value={value.buffer_buy_gb}
                onChange={(event) => {
                  const next = wholeNumber(event.target.value);
                  if (next !== null && next >= 50)
                    onChange({ ...value, buffer_buy_gb: next });
                }}
              />
            </label>
          </div>
        )}
      </div>
      <div className="helper-option">
        <div className="helper-toggle">
          <label className="check-label">
            <input
              type="checkbox"
              checked={value.spend_bonus}
              onChange={(event) =>
                onChange({ ...value, spend_bonus: event.target.checked })
              }
            />
            Spend excess bonus points
          </label>
          <SettingHelp label="bonus points">
            Buys upload credit while bonus points are above this number. Stops
            when the balance does not fall, or when it reaches the threshold.
          </SettingHelp>
        </div>
        {value.spend_bonus && (
          <div className="automation-fields">
            <label>
              If bonus points exceed
              <input
                type="number"
                min={0}
                max={100000000}
                value={value.bonus_above}
                onChange={(event) => {
                  const next = wholeNumber(event.target.value);
                  if (next !== null && next >= 0)
                    onChange({ ...value, bonus_above: next });
                }}
              />
            </label>
            <label>
              Buy (GB)
              <input
                type="number"
                min={50}
                max={100000}
                value={value.bonus_buy_gb}
                onChange={(event) => {
                  const next = wholeNumber(event.target.value);
                  if (next !== null && next >= 50)
                    onChange({ ...value, bonus_buy_gb: next });
                }}
              />
            </label>
          </div>
        )}
      </div>
      {(value.protect_ratio || value.maintain_buffer || value.spend_bonus) && (
        <label>
          Check interval (hours)
          <input
            type="number"
            min={1}
            max={168}
            value={value.upload_interval_hours}
            onChange={(event) => {
              const next = wholeNumber(event.target.value);
              if (next !== null && next >= 1 && next <= 168)
                onChange({ ...value, upload_interval_hours: next });
            }}
          />
        </label>
      )}
    </details>
  );
}

function MamCheckError({ message }: { message?: string | null }) {
  if (!message) return null;
  const summary = message.startsWith("Direct IP check failed.")
    ? "Direct IP unavailable. Check the server's internet connection."
    : /destination hostname/i.test(message)
      ? "MAM address not found. Check the MAM URL and server DNS."
      : /proxy.*authentication|HTTPS tunnel/i.test(message)
        ? "Proxy rejected the connection. Check its credentials and settings."
        : /hostname.*resolved|DNS/i.test(message)
          ? "Proxy not found. Connect Dewarr and Gluetun to the same Docker network."
          : /refused/i.test(message)
            ? "Proxy connection refused. Check its address and port."
            : /timed? out|timeout/i.test(message)
              ? "Connection timed out. Check the proxy and try again."
              : /TLS|certificate/i.test(message)
                ? "Secure connection failed. Check the proxy URL and certificates."
                : /rejected.*session|cookie|mam_id|authentication/i.test(
                      message,
                    )
                  ? "MAM sign-in failed. Check your mam_id and its allowed IP."
                  : message.length <= 120
                    ? message
                    : "Connection failed. Check your settings and try again.";
  return (
    <div className="mam-check-error">
      <p>{summary}</p>
      {summary !== message && (
        <details>
          <summary>Error details</summary>
          <p>{message}</p>
        </details>
      )}
    </div>
  );
}

export function MamConnectionForm({ value }: { value: Connection }) {
  const cache = useQueryClient();
  const [base, setBase] = useState(value.base_url);
  const [proxy, setProxy] = useState(value.proxy_url || "");
  const [proxyFallback, setProxyFallback] = useState(
    value.proxy_fallback_direct,
  );
  const [cookie, setCookie] = useState("");
  const [retainedCookie, setRetainedCookie] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [clearAuth, setClearAuth] = useState(false);
  const [enabled, setEnabled] = useState(
    value.configured ? value.enabled : true,
  );
  const savedAutomation = automationSettings(value);
  const [automation, setAutomation] = useState(savedAutomation);
  const networkDirty =
    proxy !== (value.proxy_url || "") ||
    Boolean(username || password || clearAuth);
  const accountDirty =
    networkDirty ||
    base !== value.base_url ||
    proxyFallback !== value.proxy_fallback_direct ||
    Boolean(cookie && cookie !== retainedCookie);
  const dirty =
    accountDirty ||
    enabled !== value.enabled ||
    JSON.stringify(automation) !== JSON.stringify(savedAutomation);
  const network = useQuery<components["schemas"]["MAMNetworkView"] | null>({
    queryKey: ["mam-network", value.generation],
    queryFn: async () => null,
    enabled: false,
  });
  const persist = async () => {
    const connection = result(
      await api.PUT("/api/sources/mam/connection", {
        body: {
          base_url: base,
          proxy_url: proxy || null,
          proxy_fallback_direct: proxyFallback,
          mam_id: cookie || null,
          proxy_username: username || null,
          proxy_password: password || null,
          clear_proxy_credentials: clearAuth,
          enabled,
          automation,
          expected_generation: value.generation,
        },
      }),
    );
    // Account-only edits do not invalidate a successful proxy/IP check.
    if (!networkDirty && network.data) {
      cache.setQueryData(["mam-network", connection.generation], network.data);
    }
    setRetainedCookie(cookie || null);
    setUsername("");
    setPassword("");
    setClearAuth(false);
    return connection;
  };
  const save = useMutation({
    mutationFn: persist,
    onSuccess: (connection) => {
      setCookie("");
      setRetainedCookie(null);
      cache.setQueryData(["mam-connection"], connection);
    },
  });
  const proxyTest = useMutation({
    mutationFn: async () => {
      if (dirty) await persist();
      return result(
        await api.POST("/api/sources/mam/network/test", {
          params: { query: { include_cookie: false } },
        }),
      );
    },
    onSuccess: (diagnostics) => {
      cache.setQueryData(
        ["mam-network", diagnostics.connection.generation],
        diagnostics,
      );
      cache.setQueryData(["mam-connection"], diagnostics.connection);
    },
    onError: () => cache.invalidateQueries({ queryKey: ["mam-connection"] }),
  });
  const cookieTest = useMutation({
    mutationFn: async () => {
      if (dirty) await persist();
      return result(await api.POST("/api/sources/mam/connection/test"));
    },
    onSuccess: (connection) => {
      setCookie("");
      setRetainedCookie(null);
      cache.setQueryData(["mam-connection"], connection);
    },
    onError: () => cache.invalidateQueries({ queryKey: ["mam-connection"] }),
  });
  const busy = save.isPending || proxyTest.isPending || cookieTest.isPending;
  const health = !networkDirty && enabled ? network.data : null;
  const proxyError = proxyTest.error?.message || health?.proxy?.error;
  const accountError =
    cookieTest.error?.message || (!accountDirty ? value.last_error : null);
  const accountStatus = !enabled
    ? "Disabled"
    : accountDirty
      ? "Not tested"
      : value.status === "connected"
        ? "Authenticated"
        : value.status === "authentication"
          ? "Rejected"
          : "Unverified";
  return (
    <form
      className="panel editor mam-setup"
      aria-label="MAM connection settings"
      onChange={() => {
        save.reset();
        proxyTest.reset();
        cookieTest.reset();
      }}
      onSubmit={(event) => {
        event.preventDefault();
        if (!busy) save.mutate();
      }}
    >
      <fieldset className="mam-setup-fields" disabled={busy}>
        <label className="check-label">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(event) => setEnabled(event.target.checked)}
          />
          Enable MAM
        </label>
        <div className="mam-setup-checks">
          <section
            className="mam-network"
            aria-label="MAM network status"
            data-health={
              proxyError
                ? "unhealthy"
                : (proxy ? health?.proxy?.ip : health?.direct.ip)
                  ? "healthy"
                  : "unknown"
            }
          >
            <div className="mam-network-heading">
              <h3>Proxy</h3>
              <span className="mam-network-badge" role="status">
                {proxyTest.isPending
                  ? "Testing…"
                  : proxyError
                    ? "Unavailable"
                    : health?.proxy_status === "healthy"
                      ? "Healthy"
                      : proxy
                        ? "Not tested"
                        : "Direct"}
              </span>
            </div>
            <label>
              HTTP proxy URL
              <input
                type="url"
                value={proxy}
                onChange={(event) => setProxy(event.target.value)}
                placeholder="http://gluetun:8888"
                maxLength={2000}
              />
            </label>
            <p className="mam-check-hint">
              Optional. Check the proxy and public IPs without signing in to
              MAM.
            </p>
            <button
              type="button"
              disabled={!enabled}
              onClick={(event) => {
                if (event.currentTarget.form?.reportValidity())
                  proxyTest.mutate();
              }}
            >
              {proxyTest.isPending
                ? "Testing proxy…"
                : proxy
                  ? "Test proxy"
                  : "Test network"}
            </button>
            <div aria-live="polite">
              <MamCheckError message={proxyError} />
              <dl>
                <div className="mam-network-address">
                  <dt>Proxy IP</dt>
                  <dd>
                    {health?.proxy?.ip ||
                      (proxyError
                        ? "Unavailable"
                        : proxy
                          ? "Not tested"
                          : "Not configured")}
                  </dd>
                </div>
                <div className="mam-network-address">
                  <dt>Direct server IP</dt>
                  <dd>
                    {health?.direct.ip ||
                      (health?.direct.error ? "Unavailable" : "Not tested")}
                  </dd>
                </div>
              </dl>
              <MamCheckError
                message={
                  health?.direct.error
                    ? `Direct IP check failed. ${health.direct.error}`
                    : null
                }
              />
              {health && (
                <p className="mam-network-checked">
                  Last checked{" "}
                  <time dateTime={health.checked_at}>
                    {new Date(health.checked_at).toLocaleTimeString()}
                  </time>
                </p>
              )}
              {health?.proxy?.ip && health.proxy.ip === health.direct.ip && (
                <p className="mam-check-hint">
                  Both routes report the same IP. Check the VPN if you expect
                  different addresses.
                </p>
              )}
            </div>
          </section>
          <section
            className="mam-network"
            aria-label="MAM account check"
            data-health={
              accountError
                ? "unhealthy"
                : accountStatus === "Authenticated"
                  ? "healthy"
                  : "unknown"
            }
          >
            <div className="mam-network-heading">
              <h3>MAM account</h3>
              <span
                className="mam-network-badge"
                role="status"
                aria-label="Connection test status"
              >
                {cookieTest.isPending
                  ? "Testing…"
                  : accountError
                    ? "Failed"
                    : accountStatus}
              </span>
            </div>
            <label>
              mam_id
              <input
                type="password"
                value={cookie}
                onChange={(event) => setCookie(event.target.value)}
                autoComplete="new-password"
                placeholder={
                  value.has_session ? "••••••••" : "Session cookie value"
                }
                maxLength={8192}
              />
            </label>
            <p className="mam-check-hint">
              Verify your cookie using{" "}
              {proxy
                ? proxyFallback
                  ? "the proxy, with direct fallback"
                  : "the proxy only"
                : "the direct connection"}
              .
            </p>
            <button
              type="button"
              disabled={!enabled || !(value.has_session || cookie)}
              onClick={(event) => {
                if (event.currentTarget.form?.reportValidity())
                  cookieTest.mutate();
              }}
            >
              {cookieTest.isPending ? "Testing mam_id…" : "Test mam_id"}
            </button>
            <div aria-live="polite">
              <MamCheckError message={accountError} />
            </div>
          </section>
        </div>
        <div className="actions">
          <button className="primary">Save connection</button>
          {value.configured && (
            <DeleteSourceConnection
              source="mam"
              name="MAM"
              generation={value.generation}
              disabled={busy}
            />
          )}
          {dirty && (
            <span className="mam-check-hint">
              Tests save your changes first.
            </span>
          )}
        </div>
        <MamCheckError message={save.error?.message} />
        <details className="mam-advanced">
          <summary>Advanced settings</summary>
          <div className="mam-advanced-fields">
            <label>
              MAM URL
              <input
                type="url"
                value={base}
                onChange={(event) => setBase(event.target.value)}
                required
                maxLength={2000}
              />
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={proxyFallback}
                onChange={(event) => setProxyFallback(event.target.checked)}
              />
              Allow direct fallback when the proxy is unavailable
            </label>
            {proxyFallback && proxy && (
              <p className="mam-check-hint">
                MAM will see the direct server IP if the proxy fails.
              </p>
            )}
            <div className="settings-fields">
              <label>
                Proxy username
                <input
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  autoComplete="off"
                  maxLength={300}
                />
              </label>
              <label>
                Proxy password
                <input
                  type="password"
                  placeholder={
                    value.has_proxy_credentials &&
                    !clearAuth &&
                    proxy === (value.proxy_url || "")
                      ? "••••••••"
                      : undefined
                  }
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete="new-password"
                  maxLength={1000}
                />
              </label>
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={clearAuth}
                  onChange={(event) => setClearAuth(event.target.checked)}
                />
                Clear saved proxy credentials
              </label>
            </div>
          </div>
        </details>
        <AccountAutomation value={automation} onChange={setAutomation} />
      </fieldset>
    </form>
  );
}
