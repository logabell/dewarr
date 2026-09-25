import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, result, setCsrf, type Auth } from "../api/client";
import type { components } from "../api/schema";
import BookDialog from "../components/BookDialog";
import { Link } from "react-router-dom";
import { Loading, Notice } from "../components";
import {
  AccessDialog,
  sameIds,
  useAccessLibraries,
  UserLibraries,
} from "./AccessControls";

type User = components["schemas"]["UserView"];
type Catalog = components["schemas"]["AccessCatalog"];
type Role = components["schemas"]["RoleView"];
type PresetRole = "admin" | "member" | "viewer" | "requester" | "approver";
type Editor =
  | { kind: "add" }
  | { kind: "user"; id: string }
  | { kind: "role"; id: string | null };

const presetRoles = new Set<PresetRole>([
  "admin",
  "member",
  "viewer",
  "requester",
  "approver",
]);

type OidcSettings = components["schemas"]["OidcSettingsView"];
type PlexSettings = components["schemas"]["PlexSettingsView"];
type PlexServer = components["schemas"]["PlexServerView"];

const OIDC_SETUP_GUIDE =
  "https://github.com/logabell/dewarr/blob/dev/docs/OIDC.md";
const PLEX_SETUP_GUIDE =
  "https://github.com/logabell/dewarr/blob/dev/docs/PLEX.md";

const PLEX_ERRORS: Record<string, string> = {
  denied: "Plex did not sign you in.",
  mismatch: "That sign-in attempt expired. Try again.",
  rejected: "This Plex account cannot sign in.",
  unavailable: "Plex could not be reached.",
  paused: "Sign-in is paused during recovery review.",
  limited: "Too many sign-in attempts. Try again in ten minutes.",
};

function OidcSettingsPanel() {
  const client = useQueryClient();
  const settings = useQuery({
    queryKey: ["oidc-settings"],
    queryFn: async () => result(await api.GET("/api/auth/oidc/settings")),
  });
  const [draft, setDraft] = useState<OidcSettings | null>(null);
  const [secret, setSecret] = useState("");
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (settings.data) {
      setDraft(settings.data);
      setSecret("");
    }
  }, [settings.data]);
  const save = useMutation({
    mutationFn: async () => {
      if (!draft) return;
      const { secret_set, redirect_uri, ...fields } = draft;
      void secret_set;
      void redirect_uri;
      return result(
        await api.PUT("/api/auth/oidc/settings", {
          body: { ...fields, client_secret: secret },
        }),
      );
    },
    onSuccess: (saved) => {
      if (!saved) return;
      client.setQueryData(["oidc-settings"], saved);
      setSecret("");
    },
  });
  const discover = useMutation({
    mutationFn: async () => {
      if (!draft?.issuer) throw new Error("Enter the issuer URL first");
      return result(
        await api.POST("/api/auth/oidc/discover", {
          body: { issuer: draft.issuer },
        }),
      );
    },
    onSuccess: (found) =>
      setDraft((current) => (current ? { ...current, ...found } : current)),
  });
  const update = (key: keyof OidcSettings, value: string | boolean) =>
    setDraft((current) =>
      current ? ({ ...current, [key]: value } as OidcSettings) : current,
    );
  useEffect(() => {
    if (settings.error || discover.error || save.error) setOpen(true);
  }, [settings.error, discover.error, save.error]);
  const endpointsReady = Boolean(
    draft?.authorization_endpoint &&
    draft.token_endpoint &&
    draft.userinfo_endpoint &&
    draft.jwks_uri,
  );
  return (
    <details
      className="access-provider"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <h2>Identity provider</h2>
        {draft ? (
          <span className="connection-state">
            {draft.enabled ? "On" : "Off"}
          </span>
        ) : null}
        <a
          className="settings-inline-link"
          href={OIDC_SETUP_GUIDE}
          target="_blank"
          rel="noreferrer"
        >
          Setup guide
        </a>
      </summary>
      <form
        className="panel editor"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <p className="muted">
          Sign in with Authentik, Pocket ID, Authelia, or another OpenID Connect
          provider. Local passwords stay available.
        </p>
        {settings.isPending ? (
          <Loading />
        ) : draft ? (
          <>
            <label>
              Redirect URL
              <input
                className="oidc-redirect"
                readOnly
                value={draft.redirect_uri}
              />
            </label>
            <label className="check-label">
              <input
                type="checkbox"
                checked={draft.enabled}
                onChange={(event) => update("enabled", event.target.checked)}
              />
              Enable identity provider sign-in
            </label>
            <div className="form-row">
              <label>
                Button label
                <input
                  value={draft.label}
                  maxLength={80}
                  required
                  onChange={(event) => update("label", event.target.value)}
                />
              </label>
              <label>
                Issuer URL
                <input
                  value={draft.issuer}
                  inputMode="url"
                  autoComplete="off"
                  onChange={(event) => update("issuer", event.target.value)}
                />
              </label>
            </div>
            <button
              type="button"
              disabled={discover.isPending || !draft.issuer}
              onClick={() => discover.mutate()}
            >
              {discover.isPending ? "Discovering…" : "Discover endpoints"}
            </button>
            <div className="form-row">
              <label>
                Client ID
                <input
                  value={draft.client_id}
                  autoComplete="off"
                  onChange={(event) => update("client_id", event.target.value)}
                />
              </label>
              <label>
                Client secret
                <input
                  type="password"
                  value={secret}
                  autoComplete="new-password"
                  placeholder={
                    draft.secret_set
                      ? "Saved — leave blank to keep it"
                      : "From the identity provider"
                  }
                  onChange={(event) => setSecret(event.target.value)}
                />
              </label>
            </div>
            <div className="form-row">
              <label>
                Match existing accounts
                <select
                  value={draft.match_existing}
                  onChange={(event) =>
                    update("match_existing", event.target.value)
                  }
                >
                  <option value="off">Do not match</option>
                  <option value="username">Username</option>
                  <option value="email">Verified email</option>
                </select>
              </label>
              <label>
                New account access
                <select
                  value={draft.default_role}
                  onChange={(event) =>
                    update("default_role", event.target.value)
                  }
                >
                  <option value="member">Member</option>
                  <option value="viewer">Viewer</option>
                </select>
              </label>
            </div>
            <label className="check-label">
              <input
                type="checkbox"
                checked={draft.auto_register}
                onChange={(event) =>
                  update("auto_register", event.target.checked)
                }
              />
              Create accounts on first sign-in
            </label>
            <details className="access-provider-advanced">
              <summary>
                <span>Endpoints</span>
                <span className="connection-state">
                  {endpointsReady ? "Discovered" : "Not discovered"}
                </span>
              </summary>
              <div className="form-row">
                <label>
                  Authorize URL
                  <input
                    value={draft.authorization_endpoint}
                    onChange={(event) =>
                      update("authorization_endpoint", event.target.value)
                    }
                  />
                </label>
                <label>
                  Token URL
                  <input
                    value={draft.token_endpoint}
                    onChange={(event) =>
                      update("token_endpoint", event.target.value)
                    }
                  />
                </label>
              </div>
              <div className="form-row">
                <label>
                  Userinfo URL
                  <input
                    value={draft.userinfo_endpoint}
                    onChange={(event) =>
                      update("userinfo_endpoint", event.target.value)
                    }
                  />
                </label>
                <label>
                  JWKS URL
                  <input
                    value={draft.jwks_uri}
                    onChange={(event) => update("jwks_uri", event.target.value)}
                  />
                </label>
              </div>
              <label>
                Signing algorithm
                <select
                  value={draft.signing_algorithm}
                  onChange={(event) =>
                    update("signing_algorithm", event.target.value)
                  }
                >
                  <option value="RS256">RS256</option>
                  <option value="ES256">ES256</option>
                </select>
              </label>
            </details>
            <details className="access-provider-advanced">
              <summary>
                <span>Groups</span>
                <span className="connection-state">Optional</span>
              </summary>
              <div className="form-row">
                <label>
                  Group claim
                  <input
                    value={draft.group_claim}
                    placeholder="groups"
                    onChange={(event) =>
                      update("group_claim", event.target.value)
                    }
                  />
                </label>
                <label>
                  Group scope
                  <input
                    value={draft.group_scope}
                    placeholder="Only if the provider requires it"
                    onChange={(event) =>
                      update("group_scope", event.target.value)
                    }
                  />
                </label>
              </div>
              <div className="form-row">
                <label>
                  Administrator group
                  <input
                    value={draft.admin_group}
                    onChange={(event) =>
                      update("admin_group", event.target.value)
                    }
                  />
                </label>
                <label>
                  Member group
                  <input
                    value={draft.member_group}
                    onChange={(event) =>
                      update("member_group", event.target.value)
                    }
                  />
                </label>
              </div>
              <div className="form-row">
                <label>
                  Viewer group
                  <input
                    value={draft.viewer_group}
                    onChange={(event) =>
                      update("viewer_group", event.target.value)
                    }
                  />
                </label>
              </div>
            </details>
            <button className="primary" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save identity provider"}
            </button>
          </>
        ) : null}
        <Notice error={settings.error || discover.error || save.error} />
      </form>
    </details>
  );
}

function PlexSettingsPanel() {
  const client = useQueryClient();
  const settings = useQuery({
    queryKey: ["plex-settings"],
    queryFn: async () => result(await api.GET("/api/auth/plex/settings")),
  });
  const pending = useQuery({
    queryKey: ["plex-pending"],
    queryFn: async () => result(await api.GET("/api/auth/plex/pending")),
  });
  const [draft, setDraft] = useState<PlexSettings | null>(null);
  const [machineId, setMachineId] = useState("");
  const [plexError] = useState(() => {
    const code = new URLSearchParams(window.location.search).get("plex_error");
    return code ? PLEX_ERRORS[code] : "";
  });
  const [open, setOpen] = useState(Boolean(plexError));
  useEffect(() => {
    if (!plexError) return;
    const url = new URL(window.location.href);
    url.searchParams.delete("plex_error");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }, [plexError]);
  useEffect(() => {
    if (!settings.data) return;
    setDraft(settings.data);
    setMachineId((current) => current || settings.data.machine_id);
  }, [settings.data]);
  useEffect(() => {
    const servers = pending.data?.servers ?? [];
    if (!servers.length) return;
    setMachineId((current) =>
      servers.some((server) => server.machine_id === current)
        ? current
        : servers[0].machine_id,
    );
  }, [pending.data]);
  const save = useMutation({
    mutationFn: async () => {
      if (!draft) return;
      return result(
        await api.PUT("/api/auth/plex/settings", {
          body: {
            enabled: draft.enabled,
            machine_id: machineId,
            auto_register: draft.auto_register,
            default_role: draft.default_role,
          },
        }),
      );
    },
    onSuccess: (saved) => {
      if (!saved) return;
      client.setQueryData(["plex-settings"], saved);
      client.setQueryData(["plex-pending"], { servers: [] });
    },
  });
  const update = (key: keyof PlexSettings, value: string | boolean) =>
    setDraft((current) =>
      current ? ({ ...current, [key]: value } as PlexSettings) : current,
    );
  const servers: PlexServer[] = pending.data?.servers ?? [];
  useEffect(() => {
    if (settings.error || pending.error || save.error || plexError)
      setOpen(true);
  }, [settings.error, pending.error, save.error, plexError]);
  return (
    <details
      className="access-provider"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <h2>Plex</h2>
        {draft ? (
          <span className="connection-state">
            {draft.enabled ? "On" : "Off"}
          </span>
        ) : null}
        <a
          className="settings-inline-link"
          href={PLEX_SETUP_GUIDE}
          target="_blank"
          rel="noreferrer"
        >
          Setup guide
        </a>
      </summary>
      <form
        className="panel editor"
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <p className="muted">
          Let people who can access one Plex server sign in with that account.
          Local passwords stay available.
        </p>
        {settings.isPending ? (
          <Loading />
        ) : draft ? (
          <>
            <label className="check-label">
              <input
                type="checkbox"
                checked={draft.enabled}
                disabled={!machineId}
                onChange={(event) => update("enabled", event.target.checked)}
              />
              Enable Plex sign-in
            </label>
            <a className="plex-link" href="/api/auth/plex/link">
              Link a Plex server
            </a>
            {draft.machine_id ? (
              <p className="muted">Linked server: {draft.server_name}</p>
            ) : servers.length ? (
              <p className="muted">
                Choose this server and save to turn on Plex sign-in.
              </p>
            ) : (
              <p className="muted">No Plex server linked yet.</p>
            )}
            {servers.length ? (
              <label>
                Plex server
                <select
                  value={machineId}
                  onChange={(event) => setMachineId(event.target.value)}
                >
                  {servers.map((server) => (
                    <option key={server.machine_id} value={server.machine_id}>
                      {server.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <div className="form-row">
              <label>
                New account access
                <select
                  value={draft.default_role}
                  onChange={(event) =>
                    update("default_role", event.target.value)
                  }
                >
                  <option value="member">Member</option>
                  <option value="viewer">Viewer</option>
                </select>
              </label>
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={draft.auto_register}
                  onChange={(event) =>
                    update("auto_register", event.target.checked)
                  }
                />
                Create accounts on first sign-in
              </label>
            </div>
            <button className="primary" disabled={save.isPending}>
              {save.isPending ? "Saving…" : "Save Plex sign-in"}
            </button>
          </>
        ) : null}
        <Notice
          error={
            settings.error ||
            pending.error ||
            save.error ||
            (plexError ? new Error(plexError) : null)
          }
        />
      </form>
    </details>
  );
}

export default function Accounts({ embedded = false }: { embedded?: boolean }) {
  const client = useQueryClient();
  const session = useQuery({
    queryKey: ["session"],
    queryFn: async () => {
      const response = await api.GET("/api/auth/me");
      if (response.response.status === 401) return null;
      const auth = result(response);
      setCsrf(auth.csrf_token);
      return auth;
    },
  });
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: async () => result(await api.GET("/api/auth/users")),
  });
  const catalog = useQuery({
    queryKey: ["access-catalog"],
    queryFn: async () => result(await api.GET("/api/auth/access")),
  });
  const [panel, setPanel] = useState<"users" | "roles">("users");
  const usersTab = useRef<HTMLButtonElement>(null);
  const rolesTab = useRef<HTMLButtonElement>(null);
  function moveTab(next: "users" | "roles") {
    setPanel(next);
    (next === "users" ? usersTab : rolesTab).current?.focus();
  }
  const [editor, setEditor] = useState<Editor | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const actor = (session.data as Auth | null | undefined)?.user;
  const selfId = actor?.id;
  const held = actor?.role === "admin" ? null : (actor?.permissions ?? []);
  const people = [...(accounts.data ?? [])].sort((a, b) => {
    if (a.id === selfId) return -1;
    if (b.id === selfId) return 1;
    return a.display_name.localeCompare(b.display_name);
  });
  const editing = people.find(
    (user) => user.id === (editor?.kind === "user" ? editor.id : ""),
  );
  const editingRole =
    editor?.kind === "role" && editor.id
      ? catalog.data?.roles.find((role) => role.id === editor.id)
      : null;

  return (
    <div className="access-page">
      {!embedded && (
        <div className="page-heading">
          <div>
            <p className="eyebrow">YOUR HOUSEHOLD</p>
            <h1>Accounts</h1>
          </div>
        </div>
      )}
      <div className="access-toolbar">
        <div
          className="access-switch"
          role="tablist"
          aria-label="Users and roles"
        >
          <button
            ref={usersTab}
            type="button"
            role="tab"
            id="access-users-tab"
            aria-selected={panel === "users"}
            aria-controls="access-users-panel"
            tabIndex={panel === "users" ? 0 : -1}
            onClick={() => setPanel("users")}
            onKeyDown={(event) => {
              if (event.key !== "ArrowRight" && event.key !== "ArrowLeft")
                return;
              event.preventDefault();
              moveTab(event.key === "ArrowRight" ? "roles" : "users");
            }}
          >
            Users
          </button>
          <button
            ref={rolesTab}
            type="button"
            role="tab"
            id="access-roles-tab"
            aria-selected={panel === "roles"}
            aria-controls="access-roles-panel"
            tabIndex={panel === "roles" ? 0 : -1}
            onClick={() => setPanel("roles")}
            onKeyDown={(event) => {
              if (event.key !== "ArrowRight" && event.key !== "ArrowLeft")
                return;
              event.preventDefault();
              moveTab(event.key === "ArrowRight" ? "roles" : "users");
            }}
          >
            Roles
          </button>
        </div>
        {panel === "users" ? (
          <button
            type="button"
            className="primary"
            disabled={!catalog.data}
            onClick={() => {
              setBanner(null);
              setEditor({ kind: "add" });
            }}
          >
            Add user
          </button>
        ) : (
          <button
            type="button"
            className="primary"
            disabled={!catalog.data}
            onClick={() => {
              setBanner(null);
              setEditor({ kind: "role", id: null });
            }}
          >
            New role
          </button>
        )}
      </div>
      <p className="access-lead">
        {panel === "users"
          ? "Set each person’s role and libraries together. Roles control actions; libraries control content."
          : "Built-in roles are ready to assign. A custom role is a shared set you can edit in one place."}
      </p>
      {actor?.role === "admin" && (
        <p className="muted">
          You can also manage access by library in{" "}
          <Link to="/settings#libraries">Libraries</Link>. New Plex accounts
          appear here after their first sign-in; edit them to choose libraries.
        </p>
      )}
      <Notice error={accounts.error || catalog.error} />
      {banner && <Notice error={new Error(banner)} />}
      {accounts.isPending ? (
        <Loading />
      ) : panel === "users" ? (
        <div
          id="access-users-panel"
          role="tabpanel"
          aria-labelledby="access-users-tab"
        >
          <UsersTable
            people={people}
            selfId={selfId}
            canManageAdmins={actor?.role === "admin"}
            onEdit={(id) => {
              setBanner(null);
              setEditor({ kind: "user", id });
            }}
          />
        </div>
      ) : catalog.isPending ? (
        <Loading />
      ) : (
        catalog.data && (
          <div
            id="access-roles-panel"
            role="tabpanel"
            aria-labelledby="access-roles-tab"
          >
            <RolesTable
              catalog={catalog.data}
              people={people}
              onEdit={(id) => {
                setBanner(null);
                setEditor({ kind: "role", id });
              }}
            />
          </div>
        )
      )}
      {editor?.kind === "add" && catalog.data && (
        <AddUserDialog
          catalog={catalog.data}
          held={held}
          close={() => setEditor(null)}
          onLinked={(message) => setBanner(message)}
        />
      )}
      {editor?.kind === "user" && catalog.data && editing && (
        <EditUserDialog
          key={editing.id}
          user={editing}
          catalog={catalog.data}
          held={held}
          selfId={selfId}
          close={() => setEditor(null)}
        />
      )}
      {editor?.kind === "role" &&
        catalog.data &&
        (editor.id === null || editingRole) && (
          <RoleDialog
            key={editingRole?.id ?? "new"}
            role={editingRole ?? null}
            catalog={catalog.data}
            held={held}
            close={() => setEditor(null)}
            onSaved={() => client.invalidateQueries({ queryKey: ["accounts"] })}
          />
        )}
      {actor?.role === "admin" && (
        <>
          <OidcSettingsPanel />
          <PlexSettingsPanel />
        </>
      )}
    </div>
  );
}

function UsersTable({
  people,
  selfId,
  canManageAdmins,
  onEdit,
}: {
  people: User[];
  selfId?: string;
  canManageAdmins: boolean;
  onEdit: (id: string) => void;
}) {
  return (
    <div className="access-table-scroll">
      <table className="access-table access-table-users">
        <caption className="sr-only">Users</caption>
        <thead>
          <tr>
            <th scope="col">Name</th>
            <th scope="col">Username</th>
            <th scope="col">Role</th>
            <th scope="col">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {people.map((user) => (
            <tr key={user.id}>
              <td>
                <span className="access-name">{user.display_name}</span>
                {user.id === selfId && <span className="access-you">You</span>}
                <span className="access-username access-username-inline">
                  {user.username}
                </span>
              </td>
              <td className="access-username">{user.username}</td>
              <td>
                <span>{user.access_label}</span>
                {canManageAdmins && (
                  <span
                    className={`access-user-libraries ${user.role !== "admin" && !user.library_ids?.length ? "access-attention" : ""}`}
                  >
                    {user.role === "admin"
                      ? "All libraries"
                      : user.library_ids == null
                        ? "Library access unavailable"
                        : user.library_ids.length
                          ? `${user.library_ids.length} ${user.library_ids.length === 1 ? "library" : "libraries"}`
                          : "No library access"}
                  </span>
                )}
                {user.active === false && (
                  <span className="access-meta">Disabled account</span>
                )}
              </td>
              <td>
                {(canManageAdmins || user.role !== "admin") && (
                  <button
                    type="button"
                    aria-label={`Edit ${user.display_name}`}
                    onClick={() => onEdit(user.id)}
                  >
                    Edit
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RolesTable({
  catalog,
  people,
  onEdit,
}: {
  catalog: Catalog;
  people: User[];
  onEdit: (id: string) => void;
}) {
  const customPeople = people.filter(
    (user) => !user.permission_role_id && user.access_label === "Custom",
  ).length;
  return (
    <>
      <div className="access-table-scroll">
        <table className="access-table access-table-roles">
          <caption className="sr-only">Roles</caption>
          <thead>
            <tr>
              <th scope="col">Role</th>
              <th scope="col">People</th>
              <th scope="col">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {catalog.presets.map((preset) => (
              <tr key={preset.id}>
                <td>
                  <div className="access-name">{preset.label}</div>
                  <div className="access-meta">{preset.description}</div>
                </td>
                <td>{countLabel(presetPeople(people, preset.label))}</td>
                <td>
                  <span className="access-meta">Built-in</span>
                </td>
              </tr>
            ))}
            {catalog.roles.map((role) => (
              <tr key={role.id}>
                <td>
                  <div className="access-name">{role.name}</div>
                  {role.description && (
                    <div className="access-meta">{role.description}</div>
                  )}
                </td>
                <td>{countLabel(rolePeople(people, role.id))}</td>
                <td>
                  <button
                    type="button"
                    aria-label={`Edit ${role.name}`}
                    onClick={() => onEdit(role.id)}
                  >
                    Edit
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {customPeople > 0 && (
        <p className="access-lead">
          {countLabel(customPeople)} {customPeople === 1 ? "has" : "have"}{" "}
          permissions that do not match a role. Edit them under Users.
        </p>
      )}
    </>
  );
}

function AddUserDialog({
  catalog,
  held,
  close,
  onLinked,
}: {
  catalog: Catalog;
  held: string[] | null;
  close: () => void;
  onLinked: (message: string | null) => void;
}) {
  const client = useQueryClient();
  const libraries = useAccessLibraries(held === null);
  const [libraryIds, setLibraryIds] = useState<string[]>([]);
  const [displayName, setDisplayName] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [access, setAccess] = useState(() => defaultAccess(catalog, held));
  const preset = catalog.presets.find((item) => item.id === access);
  const role = catalog.roles.find((item) => item.id === access);
  const dirty = Boolean(
    displayName ||
    username ||
    password ||
    libraryIds.length ||
    access !== defaultAccess(catalog, held),
  );
  const create = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/auth/users", {
          body: {
            display_name: displayName.trim(),
            username: username.trim(),
            password,
            role:
              preset && presetRoles.has(preset.id as PresetRole)
                ? (preset.id as PresetRole)
                : "member",
            ...(role
              ? { role_id: role.id, permissions: role.permissions }
              : {}),
            ...(held === null ? { library_ids: libraryIds } : {}),
          },
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        client.invalidateQueries({ queryKey: ["accounts"] }),
        client.invalidateQueries({ queryKey: ["libraries"] }),
      ]);
      onLinked(null);
      close();
    },
  });
  return (
    <AccessDialog
      title="Add user"
      close={close}
      dirty={dirty}
      busy={create.isPending}
    >
      {(requestClose) => (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <div className="form-row">
            <label>
              Name
              <input
                value={displayName}
                disabled={create.isPending}
                required
                maxLength={120}
                autoFocus
                autoComplete="name"
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </label>
            <label>
              Username
              <input
                value={username}
                disabled={create.isPending}
                spellCheck={false}
                required
                minLength={3}
                maxLength={100}
                autoComplete="off"
                onChange={(event) => setUsername(event.target.value)}
              />
            </label>
          </div>
          <div className="form-row">
            <label>
              Password
              <input
                value={password}
                aria-label="Password"
                aria-describedby="new-user-password-hint"
                disabled={create.isPending}
                type="password"
                required
                minLength={12}
                maxLength={256}
                autoComplete="new-password"
                onChange={(event) => setPassword(event.target.value)}
              />
              <small id="new-user-password-hint">At least 12 characters.</small>
            </label>
            <label>
              Role
              <AccessSelect
                catalog={catalog}
                held={held}
                value={access}
                disabled={create.isPending}
                onChange={setAccess}
              />
            </label>
          </div>
          <p className="access-hint">
            {role?.description || preset?.description}
          </p>
          {held === null && (
            <UserLibraries
              query={libraries}
              selected={libraryIds}
              baseline={[]}
              onChange={setLibraryIds}
              administrator={(
                role?.permissions ??
                preset?.permissions ??
                []
              ).includes("admin")}
              busy={create.isPending}
            />
          )}
          <Notice error={create.error} />
          <div className="access-form-actions">
            <button type="button" onClick={requestClose}>
              Cancel
            </button>
            <button
              className="primary"
              disabled={
                create.isPending || (held === null && !libraries.isSuccess)
              }
            >
              {create.isPending ? "Adding…" : "Add user"}
            </button>
          </div>
        </form>
      )}
    </AccessDialog>
  );
}

function EditUserDialog({
  user: initialUser,
  catalog,
  held,
  selfId,
  close,
}: {
  user: User;
  catalog: Catalog;
  held: string[] | null;
  selfId?: string;
  close: () => void;
}) {
  const client = useQueryClient();
  const [user, setUser] = useState(initialUser);
  const libraries = useAccessLibraries(held === null);
  const [libraryIds, setLibraryIds] = useState(initialUser.library_ids ?? []);
  const [selected, setSelected] = useState(user.permissions);
  const [roleId, setRoleId] = useState<string | null>(
    user.permission_role_id ?? null,
  );
  const key = roleId ?? presetKey(catalog, selected) ?? "";
  const preset = catalog.presets.find((item) => `preset:${item.id}` === key);
  const role = catalog.roles.find((item) => item.id === roleId);
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/auth/users/{user_id}/permissions", {
          params: { path: { user_id: user.id } },
          body: {
            permissions: selected,
            role_id: roleId,
            expected_permissions: user.permissions,
            expected_role_id: user.permission_role_id ?? null,
            ...(held === null
              ? {
                  library_ids: libraryIds,
                  expected_library_ids: user.library_ids ?? [],
                }
              : {}),
          },
        }),
      ),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["accounts"] });
      client.invalidateQueries({ queryKey: ["libraries"] });
      if (user.id === selfId)
        client.invalidateQueries({ queryKey: ["session"] });
      close();
    },
  });
  const reload = useMutation({
    mutationFn: async () => {
      const [people, catalogData] = await Promise.all([
        api.GET("/api/auth/users").then(result),
        api.GET("/api/auth/access").then(result),
        ...(held === null ? [libraries.refetch({ throwOnError: true })] : []),
      ]);
      const current = people.find((person) => person.id === user.id);
      if (!current)
        throw new Error(
          "This account was removed. Close the editor to refresh the list.",
        );
      client.setQueryData(["accounts"], people);
      client.setQueryData(["access-catalog"], catalogData);
      return current;
    },
    onSuccess: (current) => {
      setUser(current);
      setSelected(current.permissions);
      setRoleId(current.permission_role_id ?? null);
      setLibraryIds(current.library_ids ?? []);
      save.reset();
    },
  });
  const busy = save.isPending || reload.isPending;
  const dirty =
    !sameIds(selected, user.permissions) ||
    roleId !== (user.permission_role_id ?? null) ||
    !sameIds(libraryIds, user.library_ids ?? []);
  const conflict = save.error instanceof ApiError && save.error.status === 409;
  return (
    <AccessDialog
      title={`Edit ${user.display_name}`}
      close={close}
      dirty={dirty}
      busy={busy}
    >
      {(requestClose) => (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <label>
            Role
            <select
              value={key}
              disabled={busy}
              autoFocus
              onChange={(event) => {
                const value = event.target.value;
                if (value.startsWith("preset:")) {
                  const next = catalog.presets.find(
                    (item) => item.id === value.slice("preset:".length),
                  );
                  setRoleId(null);
                  setSelected(next?.permissions ?? []);
                  return;
                }
                const next = catalog.roles.find((item) => item.id === value);
                setRoleId(value || null);
                if (next) setSelected(next.permissions);
              }}
            >
              {key === "" && <option value="">Custom</option>}
              <optgroup label="Built-in">
                {catalog.presets.map((item) => (
                  <option
                    key={item.id}
                    value={`preset:${item.id}`}
                    disabled={
                      !canGrant(held, item.permissions, user.permissions) &&
                      key !== `preset:${item.id}`
                    }
                  >
                    {item.label}
                  </option>
                ))}
              </optgroup>
              {!!catalog.roles.length && (
                <optgroup label="Custom">
                  {catalog.roles.map((item) => (
                    <option
                      key={item.id}
                      value={item.id}
                      disabled={
                        !canGrant(held, item.permissions, user.permissions) &&
                        roleId !== item.id
                      }
                    >
                      {item.name}
                    </option>
                  ))}
                </optgroup>
              )}
            </select>
          </label>
          <p className="access-hint">
            {role?.description ||
              preset?.description ||
              "These permissions apply only to this person."}
          </p>
          {held === null ? (
            <UserLibraries
              query={libraries}
              selected={libraryIds}
              baseline={user.library_ids ?? []}
              onChange={setLibraryIds}
              administrator={selected.includes("admin")}
              accountDisabled={user.active === false}
              busy={busy}
            />
          ) : (
            <p className="access-hint">
              Library access is managed by an administrator.
            </p>
          )}
          <details className="access-advanced">
            <summary>
              Customize permissions{" "}
              <span className="access-meta">
                {key === "" ? "Custom selection" : "Optional"}
              </span>
            </summary>
            <PermissionChecks
              disabled={busy}
              catalog={catalog}
              held={held}
              baseline={user.permissions}
              selected={selected}
              onChange={(next) => {
                setRoleId(null);
                setSelected(next);
              }}
            />
          </details>
          <Notice error={save.error || reload.error} />
          {conflict && (
            <div className="access-conflict">
              <p>
                Reloading replaces your draft with the latest saved role and
                library access.
              </p>
              <button
                type="button"
                disabled={busy}
                onClick={() => reload.mutate()}
              >
                {reload.isPending ? "Reloading…" : "Reload saved settings"}
              </button>
            </div>
          )}
          <div className="access-form-actions">
            <button type="button" onClick={requestClose}>
              Cancel
            </button>
            <button
              className="primary"
              disabled={
                busy ||
                conflict ||
                !dirty ||
                (!selected.length && key === "") ||
                (held === null && !libraries.isSuccess)
              }
            >
              {save.isPending ? "Saving…" : "Save changes"}
            </button>
          </div>
        </form>
      )}
    </AccessDialog>
  );
}

function RoleDialog({
  role,
  catalog,
  held,
  close,
  onSaved,
}: {
  role: Role | null;
  catalog: Catalog;
  held: string[] | null;
  close: () => void;
  onSaved: () => void;
}) {
  const client = useQueryClient();
  const [name, setName] = useState(role?.name ?? "");
  const [description, setDescription] = useState(role?.description ?? "");
  const [selected, setSelected] = useState(
    role?.permissions ??
      ["request", "request_ebook", "request_audio"].filter((name) =>
        canGrant(held, [name]),
      ),
  );
  const [confirming, setConfirming] = useState(false);
  const save = useMutation({
    mutationFn: async () => {
      const body = {
        name: name.trim(),
        description: description.trim(),
        permissions: selected,
      };
      if (!role) return result(await api.POST("/api/auth/roles", { body }));
      return result(
        await api.PUT("/api/auth/roles/{role_id}", {
          params: { path: { role_id: role.id } },
          body,
        }),
      );
    },
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["access-catalog"] });
      onSaved();
      close();
    },
  });
  const remove = useMutation({
    mutationFn: async () => {
      if (!role) return;
      result(
        await api.DELETE("/api/auth/roles/{role_id}", {
          params: { path: { role_id: role.id } },
        }),
      );
    },
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["access-catalog"] });
      onSaved();
      close();
    },
  });
  return (
    <BookDialog
      title={role ? `Edit ${role.name}` : "New role"}
      close={close}
      className="access-dialog access-dialog-wide"
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          save.mutate();
        }}
      >
        <div className="form-row">
          <label>
            Role name
            <input
              value={name}
              required
              maxLength={80}
              autoFocus
              onChange={(event) => setName(event.target.value)}
            />
          </label>
          <label>
            Description
            <input
              value={description}
              maxLength={300}
              onChange={(event) => setDescription(event.target.value)}
            />
          </label>
        </div>
        <PermissionChecks
          catalog={catalog}
          held={held}
          baseline={role?.permissions ?? []}
          selected={selected}
          onChange={setSelected}
        />
        {!selected.length && (
          <p className="access-hint">Choose at least one permission.</p>
        )}
        <Notice error={save.error || remove.error} />
        {confirming && role ? (
          <div className="access-confirm">
            <p>
              Delete {role.name}? People who use it keep the permissions they
              have now.
            </p>
            <div className="access-form-actions">
              <button type="button" onClick={() => setConfirming(false)}>
                Keep role
              </button>
              <button
                type="button"
                className="danger"
                disabled={remove.isPending}
                onClick={() => remove.mutate()}
              >
                {remove.isPending ? "Deleting…" : "Delete role"}
              </button>
            </div>
          </div>
        ) : (
          <div className="access-form-actions">
            {role && (
              <button
                type="button"
                className="access-delete"
                onClick={() => setConfirming(true)}
              >
                Delete role
              </button>
            )}
            <button type="button" onClick={close}>
              Cancel
            </button>
            <button
              className="primary"
              disabled={save.isPending || !name.trim() || !selected.length}
            >
              {save.isPending
                ? "Saving…"
                : role
                  ? "Save changes"
                  : "Create role"}
            </button>
          </div>
        )}
      </form>
    </BookDialog>
  );
}

function AccessSelect({
  catalog,
  held,
  value,
  onChange,
  disabled = false,
}: {
  catalog: Catalog;
  held: string[] | null;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <select
      disabled={disabled}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    >
      <optgroup label="Built-in">
        {catalog.presets.map((preset) => (
          <option
            key={preset.id}
            value={preset.id}
            disabled={
              !canGrant(held, preset.permissions) && value !== preset.id
            }
          >
            {preset.label}
          </option>
        ))}
      </optgroup>
      {!!catalog.roles.length && (
        <optgroup label="Custom">
          {catalog.roles.map((role) => (
            <option
              key={role.id}
              value={role.id}
              disabled={!canGrant(held, role.permissions) && value !== role.id}
            >
              {role.name}
            </option>
          ))}
        </optgroup>
      )}
    </select>
  );
}

function PermissionChecks({
  catalog,
  held,
  baseline,
  selected,
  onChange,
  disabled = false,
}: {
  catalog: Catalog;
  held: string[] | null;
  baseline: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
}) {
  const admin = selected.includes("admin");
  const groups = [...new Set(catalog.permissions.map((item) => item.group))];
  const allowed = (name: string) =>
    held === null || held.includes(name) || baseline.includes(name);
  return (
    <div className="permission-grid">
      {groups.map((group) => (
        <fieldset key={group}>
          <legend>{group}</legend>
          {catalog.permissions
            .filter((item) => item.group === group)
            .map((item) => (
              <label key={item.name}>
                <input
                  type="checkbox"
                  checked={admin || selected.includes(item.name)}
                  disabled={
                    disabled ||
                    (admin && item.name !== "admin") ||
                    (held !== null && item.name === "admin") ||
                    !allowed(item.name)
                  }
                  onChange={() =>
                    onChange(
                      togglePermission(selected, item.name, catalog, allowed),
                    )
                  }
                />
                <span>
                  {item.label}
                  <small>{item.description}</small>
                </span>
              </label>
            ))}
        </fieldset>
      ))}
    </div>
  );
}

const requestMedia = ["request_ebook", "request_audio"];
const autoMedia = ["auto_approve_ebook", "auto_approve_audio"];

function togglePermission(
  selected: string[],
  name: string,
  catalog: Catalog,
  allowed: (name: string) => boolean,
) {
  if (name === "admin") {
    return selected.includes("admin")
      ? selected.filter((value) => value !== "admin")
      : catalog.permissions.map((permission) => permission.name);
  }
  const has = selected.includes(name);
  let next = has
    ? selected.filter((value) => value !== name)
    : [...selected, name];
  if (name === "request" && has)
    next = next.filter((value) => !requestMedia.includes(value));
  if (
    requestMedia.includes(name) &&
    !has &&
    !next.includes("request") &&
    allowed("request")
  )
    next = [...next, "request"];
  if (name === "auto_approve" && !has) {
    for (const bit of autoMedia)
      if (!next.includes(bit) && allowed(bit)) next.push(bit);
  }
  if (name === "auto_approve" && has)
    next = next.filter((value) => !autoMedia.includes(value));
  if (autoMedia.includes(name) && has)
    next = next.filter((value) => value !== "auto_approve");
  return next;
}

function canGrant(
  held: string[] | null,
  permissions: string[],
  baseline: string[] = [],
) {
  if (held === null) return true;
  return permissions.every(
    (name) => held.includes(name) || baseline.includes(name),
  );
}

function defaultAccess(catalog: Catalog, held: string[] | null) {
  const presets = catalog.presets.filter((preset) =>
    canGrant(held, preset.permissions),
  );
  return (
    presets.find((preset) => preset.id === "member")?.id ??
    presets[0]?.id ??
    "viewer"
  );
}

function same(left: string[], right: string[]) {
  if (left.length !== right.length) return false;
  const values = new Set(left);
  return right.every((item) => values.has(item));
}

const automateOptional = new Set(["member", "approver", "requester"]);

function matchesPreset(
  preset: { id: string; permissions: string[] },
  selected: string[],
) {
  if (same(preset.permissions, selected)) return true;
  if (!automateOptional.has(preset.id) || !selected.includes("automate"))
    return false;
  return same(
    preset.permissions,
    selected.filter((name) => name !== "automate"),
  );
}

function presetKey(catalog: Catalog, selected: string[]) {
  const match = catalog.presets.find((preset) =>
    matchesPreset(preset, selected),
  );
  return match ? `preset:${match.id}` : null;
}

function presetPeople(people: User[], label: string) {
  return people.filter(
    (user) => !user.permission_role_id && user.access_label === label,
  ).length;
}

function rolePeople(people: User[], id: string) {
  return people.filter((user) => user.permission_role_id === id).length;
}

function countLabel(count: number) {
  return count === 1 ? "1 person" : `${count} people`;
}
