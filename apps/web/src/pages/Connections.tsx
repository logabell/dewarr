import DeleteConfiguration from "../components/DeleteConfiguration";
import Destinations from "./Destinations";
import SettingHelp from "../components/SettingHelp";
import ConnectionStatus from "../components/ConnectionStatus";
import {
  CheckCircle2,
  ChevronDown,
  XCircle,
  LoaderCircle,
  Pencil,
  RefreshCw,
  PlugZap,
  Plus,
} from "lucide-react";
import { useState, useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Empty, Loading, Notice } from "../components";
import { randomUUID } from "../randomUUID";

type Connection = components["schemas"]["ConnectionView"];

export default function Connections({
  embedded = false,
  connectionOnly = false,
}: {
  embedded?: boolean;
  connectionOnly?: boolean;
}) {
  const cache = useQueryClient();
  const [editing, setEditing] = useState<
    Connection | "audiobookshelf" | "grimmory" | null
  >(null);
  const [message, setMessage] = useState("");
  const connections = useQuery({
    queryKey: ["connections"],
    queryFn: async () => result(await api.GET("/api/integrations")),
    refetchInterval: 10000,
  });
  const libraries = useQuery({
    queryKey: ["libraries"],
    queryFn: async () => result(await api.GET("/api/library/libraries")),
    refetchInterval: 10000,
  });
  const command = useMutation({
    mutationFn: async ({
      connection,
      action,
    }: {
      connection: Connection;
      action: "test" | "sync";
    }) => {
      if (action === "test") {
        const tested = result(
          await api.POST("/api/integrations/{integration_id}/test", {
            params: { path: { integration_id: connection.id } },
          }),
        );
        setMessage(
          tested.last_error ||
            `${connection.name} connected. ${tested.scan_supported ? "Scan access available." : "Inventory access; detection uses the library watcher."}`,
        );
      } else {
        result(
          await api.POST("/api/integrations/{integration_id}/sync", {
            params: {
              path: { integration_id: connection.id },
              header: { "idempotency-key": randomUUID() },
            },
          }),
        );
        setMessage("Library sync queued. Follow its progress in Activity.");
      }
    },
    onSuccess: () => cache.invalidateQueries(),
  });
  return (
    <>
      <div className="page-heading library-page-heading">
        {!embedded && (
          <div>
            <p className="eyebrow">CONNECTED LIBRARIES</p>
            <h1>Connections</h1>
            <p className="muted">
              Keep your catalog in sync with Audiobookshelf or Grimmory.
            </p>
            <Link to="/settings#downloaders">Downloaders</Link>
          </div>
        )}
        <AddServerMenu
          emphasized={!connections.data?.length}
          onChoose={setEditing}
        />
      </div>
      <Notice error={connections.error || libraries.error || command.error} />
      {message && (
        <p className="notice" role="status">
          {message} <Link to="/settings#logs">View logs</Link>
        </p>
      )}
      {editing && (
        <ConnectionForm
          key={typeof editing === "string" ? editing : editing.id}
          kind={
            typeof editing === "string"
              ? editing
              : editing.kind === "grimmory"
                ? "grimmory"
                : "audiobookshelf"
          }
          connection={typeof editing === "string" ? undefined : editing}
          close={() => setEditing(null)}
        />
      )}
      {connections.isPending ? (
        <Loading />
      ) : connections.data?.length ? (
        <div className="connection-grid library-connections">
          {connections.data.map((connection) => (
            <article className="panel" key={connection.id}>
              <div className="section-heading">
                <h2>{connection.name}</h2>
                <ConnectionStatus
                  status={connection.enabled ? connection.status : "disabled"}
                />
              </div>
              {connection.library_count != null && (
                <p className="connection-result success">
                  <CheckCircle2 size={16} />
                  {connection.library_count} libraries · {connection.book_count}{" "}
                  books and audiobooks
                </p>
              )}
              <p className="muted break-text">{connection.base_url}</p>
              <p className="muted connection-detail">
                {connection.version ? `v${connection.version} · ` : ""}
                {connection.last_success_at
                  ? `Synced ${new Date(connection.last_success_at).toLocaleString()}`
                  : "Not synced yet"}
              </p>
              {connection.last_error && (
                <p className="notice error">{connection.last_error}</p>
              )}
              <div className="button-row">
                <button
                  className="settings-icon-button"
                  aria-label="Edit connection"
                  title="Edit connection"
                  onClick={() => setEditing(connection)}
                >
                  <Pencil size={16} />
                </button>
                <button
                  aria-label="Test connection"
                  title="Test connection"
                  disabled={!connection.enabled || command.isPending}
                  onClick={() => command.mutate({ connection, action: "test" })}
                >
                  <PlugZap size={16} /> Test
                </button>
                <button
                  aria-label="Sync library"
                  title="Sync library"
                  disabled={!connection.enabled || command.isPending}
                  onClick={() => command.mutate({ connection, action: "sync" })}
                >
                  <RefreshCw size={16} /> Sync
                </button>
                <DeleteConfiguration
                  name={connection.name}
                  disabled={command.isPending}
                  description="Remove this library connection, its saved credentials, and its library folder settings from Dewarr. Books and files in your library app are kept."
                  onDelete={async () =>
                    result(
                      await api.DELETE("/api/integrations/{integration_id}", {
                        params: { path: { integration_id: connection.id } },
                      }),
                    )
                  }
                  onDeleted={() => {
                    setEditing(null);
                    setMessage("");
                  }}
                />
              </div>
            </article>
          ))}
        </div>
      ) : embedded ? (
        <p className="muted">No libraries connected.</p>
      ) : (
        <Empty title="Bring your library into view">
          Connect Audiobookshelf or Grimmory to see which books and recordings
          you already have.
        </Empty>
      )}
      {!connectionOnly && (
        <>
          <section
            className="settings-block library-destinations"
            aria-label="Library folders"
          >
            <div className="setting-subheading">
              <h3>Library folders</h3>
              <SettingHelp label="library folders">
                Completed downloads are organized into these destinations.
                Hardlinks keep the original download available for seeding. A
                folder can rename the seeding copy in qBittorrent when you want
                the library file and the seeding file to be the same copy.
                Downloads and library folders need compatible worker mounts.
              </SettingHelp>
              <Link className="settings-inline-link" to="/settings#naming">
                File naming →
              </Link>
            </div>
            <Destinations embedded />
          </section>
        </>
      )}
    </>
  );
}

function AddServerMenu({
  emphasized,
  onChoose,
}: {
  emphasized: boolean;
  onChoose: (kind: "audiobookshelf" | "grimmory") => void;
}) {
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      trigger.current?.focus();
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  function choose(kind: "audiobookshelf" | "grimmory") {
    setOpen(false);
    onChoose(kind);
  }
  return (
    <div className="add-server-menu" ref={root}>
      <button
        ref={trigger}
        type="button"
        className={emphasized ? "primary" : undefined}
        aria-expanded={open}
        aria-haspopup="true"
        aria-controls="add-server-options"
        onClick={() => setOpen((value) => !value)}
      >
        <Plus size={14} aria-hidden="true" />
        Add server
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      {open && (
        <div
          id="add-server-options"
          className="add-server-options"
          role="group"
          aria-label="Library server"
        >
          <button type="button" onClick={() => choose("audiobookshelf")}>
            Audiobookshelf
          </button>
          <button type="button" onClick={() => choose("grimmory")}>
            Grimmory
          </button>
        </div>
      )}
    </div>
  );
}

function ConnectionForm({
  kind,
  connection,
  close,
}: {
  kind: "audiobookshelf" | "grimmory";
  connection?: Connection;
  close: () => void;
}) {
  const grimmory = kind === "grimmory";
  const appName = grimmory ? "Grimmory" : "Audiobookshelf";
  const cache = useQueryClient();
  const formRef = useRef<HTMLFormElement>(null);
  const [revision, setRevision] = useState(0);
  const [checking, setChecking] = useState(false);
  const [check, setCheck] = useState<
    components["schemas"]["ConnectionCheck"] | null
  >(null);
  const [checkError, setCheckError] = useState("");
  function bodyFrom(form: HTMLFormElement) {
    const fields = new FormData(form);
    return {
      kind,
      name: String(fields.get("name")),
      base_url: String(fields.get("base_url")),
      public_url: String(fields.get("public_url")) || null,
      token: grimmory ? null : String(fields.get("token")) || null,
      username: grimmory ? String(fields.get("username")) || null : null,
      password: grimmory ? String(fields.get("password")) || null : null,
      enabled: fields.get("enabled") === "on",
    };
  }
  useEffect(() => {
    const controller = new AbortController();
    const timer = window.setTimeout(async () => {
      const form = formRef.current;
      if (!form?.checkValidity()) return;
      setChecking(true);
      try {
        const value = result(
          await api.POST("/api/integrations/check", {
            body: bodyFrom(form),
            params: { query: { integration_id: connection?.id } },
            signal: controller.signal,
          }),
        );
        if (!controller.signal.aborted) setCheck(value);
      } catch (error) {
        if (!controller.signal.aborted)
          setCheckError(
            error instanceof Error ? error.message : "Connection failed",
          );
      } finally {
        if (!controller.signal.aborted) setChecking(false);
      }
    }, 700);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [revision, connection?.id]);
  const save = useMutation({
    mutationFn: async (form: HTMLFormElement) => {
      const body = bodyFrom(form);
      if (connection)
        result(
          await api.PUT("/api/integrations/{integration_id}", {
            params: { path: { integration_id: connection.id } },
            body,
          }),
        );
      else result(await api.POST("/api/integrations", { body }));
      form.reset();
    },
    onSuccess: async () => {
      await cache.invalidateQueries();
      close();
    },
  });
  return (
    <form
      ref={formRef}
      className="panel editor"
      onChange={() => {
        setCheck(null);
        setCheckError("");
        setChecking(false);
        setRevision((value) => value + 1);
      }}
      onSubmit={(event) => {
        event.preventDefault();
        if (check) save.mutate(event.currentTarget);
      }}
    >
      <h2>{connection ? `Edit ${appName}` : `Connect your ${appName}`}</h2>
      <Notice error={save.error} />
      <label>
        Connection name
        <input
          name="name"
          defaultValue={connection?.name || appName}
          required
          maxLength={120}
        />
      </label>
      <label>
        <span className="setting-subheading">
          Server URL{" "}
          <SettingHelp label="Server URL">
            Address reachable from this app’s server.
          </SettingHelp>
        </span>
        <input
          name="base_url"
          aria-label="Server URL"
          type="url"
          defaultValue={connection?.base_url}
          placeholder={
            grimmory ? "http://grimmory:6060" : "http://audiobookshelf:80"
          }
          required
          maxLength={2000}
        />
      </label>
      <label>
        <span className="setting-subheading">
          Browser URL (optional){" "}
          <SettingHelp label="Browser URL">
            Address for Open in {appName} links. Defaults to the server URL.
          </SettingHelp>
        </span>
        <input
          name="public_url"
          type="url"
          defaultValue={connection?.public_url}
          placeholder="https://books.example.com"
          maxLength={2000}
        />
      </label>
      {grimmory ? (
        <>
          <label>
            Username
            <input
              name="username"
              autoComplete="username"
              required={!connection}
              maxLength={200}
              placeholder={connection ? "Saved username" : "Grimmory username"}
            />
          </label>
          <label>
            Password
            <input
              name="password"
              type="password"
              autoComplete="new-password"
              required={!connection}
              maxLength={1000}
              placeholder={connection ? "••••••••" : "Grimmory password"}
            />
          </label>
        </>
      ) : (
        <label>
          API token
          <input
            name="token"
            type="password"
            autoComplete="new-password"
            required={!connection}
            maxLength={8192}
            placeholder={
              connection ? "••••••••" : "Paste your Audiobookshelf token"
            }
          />
        </label>
      )}
      <label className="check-label">
        <input
          type="checkbox"
          name="enabled"
          defaultChecked={connection?.enabled ?? true}
        />
        Enable connection
      </label>
      <div
        role="status"
        aria-live="polite"
        className={`connection-result ${check ? "success" : checkError ? "failure" : ""}`}
      >
        {checking ? (
          <>
            <LoaderCircle size={16} />
            Checking connection…
          </>
        ) : check ? (
          <>
            <CheckCircle2 size={16} />
            Connected · {check.library_count} libraries · {check.book_count}{" "}
            books and audiobooks
          </>
        ) : checkError ? (
          <>
            <XCircle size={16} />
            {checkError}
          </>
        ) : grimmory ? (
          "Enter the server, username, and password to check the connection."
        ) : (
          "Enter your server and token to check the connection."
        )}
      </div>
      <div className="button-row">
        <button
          className="primary"
          disabled={save.isPending || checking || !check}
        >
          {save.isPending ? "Saving…" : "Save connection"}
        </button>
        <button type="button" onClick={close}>
          Cancel
        </button>
      </div>
    </form>
  );
}
