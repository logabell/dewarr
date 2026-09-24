import DeleteConfiguration from "../components/DeleteConfiguration";
import {
  ArrowRight,
  CheckCircle2,
  CircleAlert,
  Download,
  Folder,
  FolderOpen,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import SlskdSettings from "./SlskdSettings";
import MountedFolderBrowser from "../components/MountedFolderBrowser";
import BookDialog from "../components/BookDialog";
import ConnectionTestStatus from "../components/ConnectionTestStatus";
import "./downloaders.css";
import SettingHelp from "../components/SettingHelp";
import { useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Empty, Loading, Notice } from "../components";

type Connection = components["schemas"]["DownloaderView"];
type DownloaderKind = Exclude<Connection["kind"], "slskd">;

const CLIENTS: Record<
  Connection["kind"],
  {
    name: string;
    placeholder: string;
    url: string;
    category: string;
    saved: string;
  }
> = {
  slskd: {
    name: "Soulseek",
    placeholder: "http://slskd:5030",
    url: "",
    category: "",
    saved: "",
  },
  qbittorrent: {
    name: "qBittorrent",
    placeholder: "http://qbittorrent:8080",
    url: "Use the Web UI address accessible to this app. This connection does not change qBittorrent’s VPN or torrent routing.",
    category: "Set download folders and torrent preferences in qBittorrent.",
    saved:
      "Credentials are stored privately. Leave both fields blank to keep them. Changing the server address clears saved credentials.",
  },
  transmission: {
    name: "Transmission",
    placeholder: "http://transmission:9091",
    url: "Use the Transmission RPC server address. Transmission 3.0 or newer is required.",
    category:
      "The category is saved as a label. The download folder comes from Transmission.",
    saved:
      "Leave credentials blank to keep them. Changing the address clears them.",
  },
  deluge: {
    name: "Deluge",
    placeholder: "http://deluge:8112",
    url: "Use the Deluge Web UI address, connected to its daemon. Enter the Web UI password; no username is needed.",
    category:
      "Use an existing Label plugin label or leave blank. Dewarr uses unique folders inside the completed download location.",
    saved:
      "Leave the password blank to keep it. Changing the address clears it.",
  },
  sabnzbd: {
    name: "SABnzbd",
    placeholder: "http://sabnzbd:8080",
    url: "Use the SABnzbd address accessible to this app. NZBs from Prowlarr are sent there. This connection does not change SABnzbd’s folders or post-processing.",
    category:
      "Set the category folder in SABnzbd. Testing this connection reads that folder.",
    saved:
      "The API key is stored privately. Leave it blank to keep the saved key. Changing the server address requires the key again.",
  },
  nzbget: {
    name: "NZBGet",
    placeholder: "http://nzbget:6789",
    url: "Use the NZBGet address accessible to this app. NZBs from Prowlarr are sent there. This connection does not change NZBGet’s folders or post-processing.",
    category:
      "Set the category folder in NZBGet. Testing this connection reads that folder.",
    saved:
      "Username and password are stored privately. Leave both blank when control authentication is off, or to keep saved credentials. Changing the server address clears them.",
  },
};

function draftKind(editing: string, selected?: Connection): DownloaderKind {
  if (selected?.kind && selected.kind !== "slskd") return selected.kind;
  if (editing === "transmission" || editing === "deluge") return editing;
  if (editing === "sab") return "sabnzbd";
  if (editing === "nzb") return "nzbget";
  return "qbittorrent";
}

export default function Downloaders({
  embedded = false,
}: {
  embedded?: boolean;
}) {
  const cache = useQueryClient();
  const [editing, setEditing] = useState<string | null>(null);
  const connections = useQuery({
    queryKey: ["downloaders"],
    queryFn: async () => result(await api.GET("/api/downloaders")),
  });
  const test = useMutation({
    mutationFn: async (id: string) =>
      result(
        await api.POST("/api/downloaders/{connection_id}/test", {
          params: { path: { connection_id: id } },
        }),
      ),
    onSettled: () =>
      Promise.all([
        cache.invalidateQueries({ queryKey: ["downloaders"] }),
        cache.invalidateQueries({ queryKey: ["setup-readiness"] }),
        cache.invalidateQueries({ queryKey: ["library-folder-options"] }),
      ]),
  });
  const saved = (connection: Connection) => {
    cache.setQueryData<Connection[]>(["downloaders"], (current = []) => [
      ...current.filter((item) => item.id !== connection.id),
      connection,
    ]);
    setEditing(null);
    if (connection.enabled) test.mutate(connection.id);
  };
  const selected = connections.data?.find(
    (connection) => connection.id === editing,
  );
  return (
    <div className="downloader-settings">
      <header className="page-heading">
        {!embedded && (
          <div>
            <p className="eyebrow">DOWNLOAD CONNECTIONS</p>
            <h1>Downloaders</h1>
            <p>
              Connect a download client. Dewarr tests the connection when you
              save.
            </p>
            <Link to="/settings#libraries">Library connections</Link>
          </div>
        )}
        <div className="button-row">
          {(
            [
              "qbittorrent",
              "transmission",
              "deluge",
              "sabnzbd",
              "nzbget",
            ] as const
          ).map((kind) => (
            <button
              key={kind}
              disabled={test.isPending}
              className={kind === "qbittorrent" ? "primary" : ""}
              onClick={() =>
                setEditing(
                  {
                    qbittorrent: "qbit",
                    sabnzbd: "sab",
                    nzbget: "nzb",
                    transmission: "transmission",
                    deluge: "deluge",
                  }[kind],
                )
              }
            >
              <Plus size={14} aria-hidden="true" /> Connect {CLIENTS[kind].name}
            </button>
          ))}
          <button disabled={test.isPending} onClick={() => setEditing("slskd")}>
            <Plus size={14} /> Connect Soulseek
          </button>
        </div>
      </header>
      <DownloadDispatchNotice />
      <Notice error={connections.error} />
      {editing === "slskd" ? (
        <BookDialog title="Soulseek connection" close={() => setEditing(null)}>
          <SlskdSettings onConfigureFolder={() => setEditing(null)} />
        </BookDialog>
      ) : (
        editing && (
          <ConnectionForm
            key={`${editing}:${selected?.generation || 0}`}
            kind={draftKind(editing, selected)}
            connection={selected}
            close={() => setEditing(null)}
            saved={saved}
          />
        )
      )}
      {connections.isPending ? (
        <Loading />
      ) : connections.data?.length ? (
        <div className="connection-grid downloader-grid">
          {connections.data.map((connection) => {
            const testing = test.isPending && test.variables === connection.id;
            const testError =
              test.variables === connection.id ? test.error : null;
            const shared =
              connection.mappings_current &&
              connection.mappings.every(
                (mapping) => mapping.download_root === mapping.worker_path,
              );
            const folderMapping = connection.mappings_current
              ? connection.mappings.find(
                  (mapping) =>
                    connection.save_path === mapping.download_root ||
                    connection.save_path.startsWith(
                      `${mapping.download_root}/`,
                    ),
                )
              : undefined;
            const localFolder = folderMapping
              ? folderMapping.worker_path +
                connection.save_path.slice(folderMapping.download_root.length)
              : undefined;
            return (
              <article
                className="panel downloader-card"
                key={connection.id}
                aria-label={connection.name}
              >
                <div className="downloader-heading">
                  <span className="downloader-icon">
                    <Download size={21} aria-hidden="true" />
                  </span>
                  <div className="downloader-identity">
                    <h2>{connection.name}</h2>
                    <p className="muted break-text">{connection.base_url}</p>
                  </div>
                  {connection.enabled ? (
                    <ConnectionTestStatus
                      configured
                      status={connection.status}
                      lastSuccessAt={connection.last_success_at}
                      isPending={testing}
                      error={testError}
                    />
                  ) : (
                    <span className="status">Disabled</span>
                  )}
                </div>
                {(testError || connection.last_error) && (
                  <div role="alert" className="notice error">
                    <strong>Connection saved. Test unsuccessful.</strong>
                    <p>{testError?.message || connection.last_error}</p>
                    <p>Check the address and credentials, then test again.</p>
                  </div>
                )}
                <dl className="downloader-facts">
                  <div>
                    <dt>Version</dt>
                    <dd>{connection.version || "Not checked"}</dd>
                  </div>
                  <div>
                    <dt>Download category</dt>
                    <dd>{connection.category || "Default (no category)"}</dd>
                  </div>
                </dl>
                <div className="downloader-folder">
                  <Folder size={18} aria-hidden="true" />
                  <div>
                    <h3>Download folder</h3>
                    {connection.save_path ? (
                      <code>{connection.save_path}</code>
                    ) : (
                      <p className="muted">
                        {testing
                          ? "Reading the folder from your download client…"
                          : "A successful connection test will detect this folder."}
                      </p>
                    )}
                    <p className="muted">
                      {connection.category
                        ? `Folder for “${connection.category}”, managed in ${CLIENTS[connection.kind].name}.`
                        : `Default folder, managed in ${CLIENTS[connection.kind].name}.`}
                    </p>
                    {localFolder && localFolder !== connection.save_path && (
                      <p className="downloader-local-folder">
                        Dewarr folder: <code>{localFolder}</code>
                      </p>
                    )}
                  </div>
                </div>
                {connection.save_path && (
                  <div
                    className="downloader-folder-status"
                    data-ready={connection.mappings_current}
                  >
                    {connection.mappings_current ? (
                      <CheckCircle2 size={16} aria-hidden="true" />
                    ) : (
                      <CircleAlert size={16} aria-hidden="true" />
                    )}
                    <div>
                      <strong>
                        {shared
                          ? "Same folder path · no translation needed"
                          : connection.mappings_current
                            ? "Folder mapping configured"
                            : "Folder setup needed"}
                      </strong>
                      <p>
                        {shared
                          ? "No manual path mapping needed. Library setup verifies file access before importing."
                          : connection.mappings_current
                            ? "Dewarr will translate the download path when importing. Library setup verifies file access."
                            : "The connection and folder setup are separate. If both apps share this path, check the volume mount and test again. If the paths differ, add a mapping below."}
                      </p>
                    </div>
                  </div>
                )}
                <PathMap
                  key={`${connection.id}:${connection.generation}:${connection.save_path}:${JSON.stringify(connection.mappings)}`}
                  connection={connection}
                  saved={saved}
                  disabled={test.isPending}
                />
                {connection.kind !== "slskd" && (
                  <details className="downloader-details">
                    <summary>Client capabilities</summary>
                    <p className="muted">
                      Attempt tags:{" "}
                      {connection.capabilities?.attempt_tagging
                        ? "supported"
                        : "unique folders identify attempts"}
                      . In-client rename:{" "}
                      {connection.capabilities?.in_client_rename
                        ? "available when enabled"
                        : "unavailable"}
                      . Categories:{" "}
                      {connection.capabilities?.categories
                        ? "supported"
                        : "Label plugin required"}
                      . Sequential/first-last controls:{" "}
                      {connection.capabilities?.sequential_first_last
                        ? "supported"
                        : "unavailable"}
                      .
                    </p>
                    {(connection.limitations || []).map((limit) => (
                      <p className="muted" key={limit}>
                        {limit}
                      </p>
                    ))}
                  </details>
                )}
                {connection.kind === "slskd" && (
                  <p className="muted">
                    Soulseek searches and downloads through the same slskd
                    connection.
                  </p>
                )}
                <div className="button-row downloader-actions">
                  <button
                    disabled={test.isPending}
                    onClick={() =>
                      setEditing(
                        connection.kind === "slskd" ? "slskd" : connection.id,
                      )
                    }
                  >
                    Edit downloader
                  </button>
                  <button
                    disabled={test.isPending || !connection.enabled}
                    onClick={() => test.mutate(connection.id)}
                  >
                    <RefreshCw size={14} aria-hidden="true" />
                    {testing ? "Testing…" : "Test connection"}
                  </button>
                  <DeleteConfiguration
                    name={connection.name}
                    disabled={test.isPending}
                    description={
                      connection.kind === "slskd"
                        ? "Remove the Soulseek search and download connection and its saved credentials. Existing files and download history are kept."
                        : "Remove this download client and its saved credentials and folder mappings. Files and transfers in the download client are kept."
                    }
                    onDelete={async () =>
                      result(
                        await api.DELETE("/api/downloaders/{connection_id}", {
                          params: {
                            path: { connection_id: connection.id },
                            query: {
                              expected_generation: connection.generation,
                            },
                          },
                        }),
                      )
                    }
                    onDeleted={() => setEditing(null)}
                  />
                </div>
              </article>
            );
          })}
        </div>
      ) : embedded ? (
        <p className="muted">No download clients connected.</p>
      ) : (
        <Empty title="No downloaders connected">
          Choose a client above. Saving automatically tests the connection and
          detects its download folder.
        </Empty>
      )}
    </div>
  );
}

function PathMap({
  connection,
  saved,
  disabled,
}: {
  connection: Connection;
  saved: (connection: Connection) => void;
  disabled: boolean;
}) {
  const [rows, setRows] = useState(() =>
    connection.mappings.map((item) => ({
      download_root: item.download_root,
      worker_path: item.worker_path,
    })),
  );
  const [browsing, setBrowsing] = useState<number | null>(null);
  const [open, setOpen] = useState(false);
  const change = (
    index: number,
    field: "download_root" | "worker_path",
    value: string,
  ) =>
    setRows((current) =>
      current.map((item, i) =>
        i === index ? { ...item, [field]: value } : item,
      ),
    );
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/downloaders/{connection_id}/mappings", {
          params: { path: { connection_id: connection.id } },
          body: { expected_generation: connection.generation, mappings: rows },
        }),
      ),
    onSuccess: saved,
  });
  if (!connection.save_path) return null;
  const dirty =
    JSON.stringify(rows) !==
    JSON.stringify(
      connection.mappings.map(({ download_root, worker_path }) => ({
        download_root,
        worker_path,
      })),
    );
  return (
    <details
      className="downloader-details downloader-mappings"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        Advanced · path mappings{" "}
        <span className="muted">
          {connection.mappings.filter((m) => m.download_root !== m.worker_path)
            .length || "Optional"}
        </span>
      </summary>
      <p className="muted">
        Only needed when the same files have different paths in your download
        client and Dewarr. Sharing a Docker Compose stack works without a
        mapping when both containers mount the downloads at the same path.
      </p>
      <p className="downloader-example">
        For example: <code>/downloads</code> in {CLIENTS[connection.kind].name}{" "}
        → <code>/data/torrents</code> in Dewarr. A mapping translates the path;
        it does not mount or move files.
      </p>
      <form
        aria-label="Download path mapping"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <fieldset disabled={disabled || save.isPending}>
          <legend className="sr-only">Folder mappings</legend>
          {rows.map((row, index) => (
            <div className="downloader-map-row" key={index}>
              <label>
                Folder in {CLIENTS[connection.kind].name}
                <input
                  aria-label={`Folder in ${CLIENTS[connection.kind].name} ${index + 1}`}
                  value={row.download_root}
                  onChange={(event) =>
                    change(index, "download_root", event.target.value)
                  }
                  required
                  maxLength={2000}
                />
              </label>
              <ArrowRight
                className="downloader-map-arrow"
                size={18}
                aria-hidden="true"
              />
              <label>
                Same folder in Dewarr
                <span className="downloader-path-input">
                  <input
                    aria-label={`Same folder in Dewarr ${index + 1}`}
                    value={row.worker_path}
                    placeholder="/data/torrents"
                    onChange={(event) =>
                      change(index, "worker_path", event.target.value)
                    }
                    required
                    maxLength={2000}
                  />
                  <button
                    type="button"
                    aria-label={`Browse Dewarr folder ${index + 1}`}
                    onClick={() => setBrowsing(index)}
                  >
                    <FolderOpen size={17} aria-hidden="true" />
                  </button>
                </span>
              </label>
              <button
                className="downloader-remove"
                type="button"
                aria-label={`Remove mapping ${index + 1}`}
                onClick={() =>
                  setRows((current) => current.filter((_, i) => i !== index))
                }
              >
                <Trash2 size={16} aria-hidden="true" />
              </button>
            </div>
          ))}
          <div className="button-row">
            <button
              type="button"
              disabled={rows.length >= 20}
              onClick={() =>
                setRows((current) => [
                  ...current,
                  {
                    download_root: current.length ? "" : connection.save_path,
                    worker_path: "",
                  },
                ])
              }
            >
              <Plus size={15} aria-hidden="true" /> Add mapping
            </button>
            {dirty && (
              <button className="primary" type="submit">
                {save.isPending
                  ? "Saving…"
                  : rows.length
                    ? "Save & test mappings"
                    : "Use automatic folder matching"}
              </button>
            )}
          </div>
          {!rows.length && (
            <p className="muted">
              Automatic matching is used when no custom mappings are saved.
            </p>
          )}
        </fieldset>
        <Notice error={save.error} />
      </form>
      {browsing !== null && (
        <DownloadFolderPicker
          close={() => setBrowsing(null)}
          select={(path) => {
            change(browsing, "worker_path", path);
            setBrowsing(null);
          }}
        />
      )}
    </details>
  );
}

function DownloadFolderPicker({
  close,
  select,
}: {
  close: () => void;
  select: (path: string) => void;
}) {
  return (
    <BookDialog
      title="Choose Dewarr download folder"
      close={close}
      className="download-folder-dialog"
    >
      <MountedFolderBrowser purpose="download" select={select} cancel={close} />
    </BookDialog>
  );
}

function ConnectionForm({
  connection,
  kind,
  close,
  saved,
}: {
  connection?: Connection;
  kind: DownloaderKind;
  close: () => void;
  saved: (connection: Connection) => void;
}) {
  const urlId = useId();
  const client = CLIENTS[kind];
  const token = kind === "sabnzbd";
  const [url, setUrl] = useState(connection?.base_url || "");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [category, setCategory] = useState(connection?.category ?? "");
  const save = useMutation({
    mutationFn: async () => {
      const body = {
        kind,
        name: connection?.name || client.name,
        base_url: url,
        username: token ? null : username || null,
        password: token ? null : password || null,
        api_key: token ? apiKey || null : null,
        category,
        enabled: connection?.enabled ?? true,
        expected_generation: connection?.generation || 0,
      };
      return connection
        ? result(
            await api.PUT("/api/downloaders/{connection_id}", {
              params: { path: { connection_id: connection.id } },
              body,
            }),
          )
        : result(await api.POST("/api/downloaders", { body }));
    },
    onSuccess: saved,
  });
  return (
    <form
      className="panel editor"
      aria-label={`${client.name} connection settings`}
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <h2>
        {connection ? "Edit" : "Connect"} {client.name}
      </h2>
      <p className="muted">
        Save to test access and detect the download folder automatically.
      </p>
      <Notice error={save.error} />
      <div style={{ display: "grid", gap: 6, maxWidth: "32rem" }}>
        <div className="setting-label">
          <label htmlFor={urlId} style={{ width: "auto" }}>
            {`${client.name} URL or IP address`}
          </label>
          <SettingHelp label={`${client.name} URL or IP address`}>
            {client.url}
          </SettingHelp>
        </div>
        <input
          id={urlId}
          type="text"
          placeholder={client.placeholder}
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          required
          maxLength={2000}
        />
      </div>
      {token ? (
        <label>
          SABnzbd API key
          <input
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder={connection?.has_credentials ? "••••••••" : undefined}
            autoComplete="new-password"
            required={!connection?.has_credentials}
            maxLength={1000}
          />
        </label>
      ) : (
        <>
          <label>
            {`${client.name} username (optional)`}
            <input
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              autoComplete="off"
              maxLength={300}
            />
          </label>
          <label>
            {`${client.name} password (optional)`}
            <input
              type="password"
              placeholder={connection?.has_credentials ? "••••••••" : undefined}
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="new-password"
              maxLength={1000}
            />
          </label>
        </>
      )}
      {connection && (
        <div className="setting-help-row">
          <SettingHelp label="connection setup">{client.saved}</SettingHelp>
        </div>
      )}
      <label>
        Download category
        <input
          value={category}
          onChange={(event) => setCategory(event.target.value)}
          pattern="[A-Za-z0-9_-]*"
          maxLength={100}
        />
      </label>
      <p className="muted">{client.category}</p>
      <div className="button-row">
        <button className="primary" disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save & test connection"}
        </button>
        <button type="button" onClick={close}>
          Cancel
        </button>
      </div>
    </form>
  );
}

function DownloadDispatchNotice() {
  const readiness = useQuery({
    queryKey: ["setup-readiness"],
    queryFn: async () => result(await api.GET("/api/setup/readiness")),
  });
  if (readiness.data?.download_dispatch_enabled !== false) return null;
  return (
    <div className="notice">
      <strong>Downloads are disabled on this server</strong>
      <p>
        You can connect clients and verify folders now. To start downloads, set{" "}
        <code>BOOK_DOWNLOAD_DISPATCH_ENABLED=true</code> in Dewarr’s environment
        and restart the container.
      </p>
    </div>
  );
}
