import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";

type Rules = components["schemas"]["Rules"] & {
  windows: NonNullable<components["schemas"]["Rules"]["windows"]>;
};
const empty: Rules = {
  windows: [],
  pending_cap: null,
  exempt_admin_approved: false,
};

export default function RequestQuotas() {
  const cache = useQueryClient();
  const [scope, setScope] = useState("installation");
  const [scopeLabel, setScopeLabel] = useState("");
  const [draft, setDraft] = useState<Rules | null>(null);
  const [offset, setOffset] = useState(0);
  const [message, setMessage] = useState("");
  const policies = useQuery({
    queryKey: ["request-quotas", "policies"],
    queryFn: async () => result(await api.GET("/api/request-quotas")),
  });
  const users = useQuery({
    queryKey: ["request-quotas", "users", offset],
    queryFn: async () =>
      result(
        await api.GET("/api/request-quotas/users", {
          params: { query: { offset, limit: 50 } },
        }),
      ),
  });
  const roles = useQuery({
    queryKey: ["access-catalog"],
    queryFn: async () => result(await api.GET("/api/auth/access")),
  });
  const stored = policies.data?.find((p) => p.scope === scope)?.rules;
  const chosen = draft || stored || empty;
  const rules: Rules = { ...chosen, windows: chosen.windows || [] };
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/request-quotas/{scope}", {
          params: { path: { scope } },
          body: rules,
        }),
      ),
    onSuccess: async () => {
      setDraft(null);
      setMessage("Request limits saved.");
      await cache.invalidateQueries({ queryKey: ["request-quotas"] });
    },
  });
  const inherit = useMutation({
    mutationFn: async () => {
      const response = await api.DELETE("/api/request-quotas/{scope}", {
        params: { path: { scope } },
      });
      if (response.error) throw new Error(String(response.error.detail));
    },
    onSuccess: async () => {
      setDraft(null);
      setMessage(
        scope === "installation"
          ? "Default limits removed."
          : "Inherited limits restored.",
      );
      await cache.invalidateQueries({ queryKey: ["request-quotas"] });
    },
  });
  const busy = save.isPending || inherit.isPending;
  const combinations = (["combined", "ebook", "audio"] as const).flatMap(
    (medium) =>
      (["week", "day", "month"] as const).map((window) => ({ medium, window })),
  );
  const nextWindow = combinations.find(
    (candidate) =>
      !rules.windows.some(
        (item) =>
          item.medium === candidate.medium && item.window === candidate.window,
      ),
  );
  const duplicate =
    new Set(rules.windows.map((item) => `${item.medium}:${item.window}`))
      .size !== rules.windows.length;
  return (
    <section className="policy-settings" aria-label="Request quotas">
      <p>
        Set a shared default, then add exceptions for roles or individual users.
        User limits replace role limits; role limits replace installation
        defaults.
      </p>
      <Notice
        error={
          policies.error ||
          users.error ||
          roles.error ||
          save.error ||
          inherit.error
        }
      />
      {policies.isPending && <Loading />}
      <form
        aria-label="Request quota settings"
        onSubmit={(event) => {
          event.preventDefault();
          if (!duplicate) save.mutate();
        }}
        onChange={() => setMessage("")}
      >
        <fieldset
          className="policy-section"
          disabled={busy || policies.isPending || !!policies.error}
        >
          <legend>Request limits</legend>
          <label>
            Apply limits to
            <select
              value={scope}
              onChange={(e) => {
                setScope(e.target.value);
                setScopeLabel(e.target.selectedOptions[0].text);
                setDraft(null);
              }}
            >
              <optgroup label="Defaults">
                <option value="installation">
                  Everyone — installation defaults
                </option>
              </optgroup>
              <optgroup label="Roles">
                <option value="role:member">Default member role</option>
                <option value="role:viewer">Default viewer role</option>
                {roles.data?.roles.map((role) => (
                  <option key={role.id} value={`role:${role.id}`}>
                    Role: {role.name}
                  </option>
                ))}
              </optgroup>
              <optgroup label="Users on this page">
                {scope.startsWith("user:") &&
                  !users.data?.some(
                    (user) => scope === `user:${user.user_id}`,
                  ) && <option value={scope}>{scopeLabel}</option>}
                {users.data?.map((user) => (
                  <option key={user.user_id} value={`user:${user.user_id}`}>
                    User: {user.user_name}
                  </option>
                ))}
              </optgroup>
            </select>
          </label>
          {!stored && scope !== "installation" && (
            <p>
              Currently uses inherited limits. These fields start unlimited;
              saving replaces the inherited policy.
            </p>
          )}
          <p className="muted">
            Blank limits are unlimited. Each requested format counts as one
            book.
          </p>
          {!rules.windows.length && (
            <p className="policy-empty">
              No rolling limits configured. Add a limit to cap books or download
              size over time.
            </p>
          )}
          {rules.windows.map((window, index) => (
            <fieldset className="policy-window" key={index}>
              <legend>Rolling limit {index + 1}</legend>
              <div className="policy-window-fields">
                <label>
                  Media
                  <select
                    value={window.medium}
                    onChange={(e) =>
                      setDraft({
                        ...rules,
                        windows: rules.windows.map((w, i) =>
                          i === index
                            ? {
                                ...w,
                                medium: e.target.value as typeof w.medium,
                              }
                            : w,
                        ),
                      })
                    }
                  >
                    <option value="combined">Combined</option>
                    <option value="ebook">Ebooks</option>
                    <option value="audio">Audiobooks</option>
                  </select>
                </label>
                <label>
                  Window
                  <select
                    value={window.window}
                    onChange={(e) =>
                      setDraft({
                        ...rules,
                        windows: rules.windows.map((w, i) =>
                          i === index
                            ? {
                                ...w,
                                window: e.target.value as typeof w.window,
                              }
                            : w,
                        ),
                      })
                    }
                  >
                    <option value="day">Day</option>
                    <option value="week">Week</option>
                    <option value="month">30 days</option>
                  </select>
                </label>
                <label>
                  Book limit
                  <input
                    type="number"
                    min="0"
                    max="1000000"
                    placeholder="Unlimited"
                    value={window.books ?? ""}
                    onChange={(e) =>
                      setDraft({
                        ...rules,
                        windows: rules.windows.map((w, i) =>
                          i === index
                            ? {
                                ...w,
                                books:
                                  e.target.value === ""
                                    ? null
                                    : Number(e.target.value),
                              }
                            : w,
                        ),
                      })
                    }
                  />
                </label>
                <label>
                  Size limit (GiB)
                  <input
                    type="number"
                    min="0"
                    step="any"
                    placeholder="Unlimited"
                    value={
                      window.size_bytes == null
                        ? ""
                        : window.size_bytes / 1024 ** 3
                    }
                    onChange={(e) =>
                      setDraft({
                        ...rules,
                        windows: rules.windows.map((w, i) =>
                          i === index
                            ? {
                                ...w,
                                size_bytes:
                                  e.target.value === ""
                                    ? null
                                    : Math.round(
                                        Number(e.target.value) * 1024 ** 3,
                                      ),
                              }
                            : w,
                        ),
                      })
                    }
                  />
                </label>
                <button
                  type="button"
                  onClick={() => {
                    setMessage("");
                    setDraft({
                      ...rules,
                      windows: rules.windows.filter((_, i) => i !== index),
                    });
                  }}
                >
                  Remove limit
                </button>
              </div>
            </fieldset>
          ))}
          <button
            type="button"
            disabled={!nextWindow}
            onClick={() => {
              setMessage("");
              setDraft({
                ...rules,
                windows: nextWindow
                  ? [
                      ...rules.windows,
                      { ...nextWindow, books: null, size_bytes: null },
                    ]
                  : rules.windows,
              });
            }}
          >
            Add rolling limit
          </button>
          {duplicate && (
            <p role="alert" className="error">
              Choose each media and window combination only once.
            </p>
          )}
        </fieldset>
        <fieldset
          className="policy-section"
          disabled={busy || policies.isPending || !!policies.error}
        >
          <legend>Approvals</legend>
          <div className="policy-fields">
            <label>
              Maximum pending approvals
              <input
                type="number"
                min="0"
                max="1000000"
                placeholder="Unlimited"
                value={rules.pending_cap ?? ""}
                onChange={(e) =>
                  setDraft({
                    ...rules,
                    pending_cap:
                      e.target.value === "" ? null : Number(e.target.value),
                  })
                }
              />
            </label>
            <label>
              <input
                type="checkbox"
                checked={rules.exempt_admin_approved}
                onChange={(e) =>
                  setDraft({
                    ...rules,
                    exempt_admin_approved: e.target.checked,
                  })
                }
              />{" "}
              Exempt requests approved by an administrator
            </label>
          </div>
        </fieldset>
        <details className="policy-disclosure">
          <summary>How quotas are counted</summary>
          <p>
            Windows roll over the last day, week, or 30 days. Existing library
            copies and shared transfers are exempt. Size is checked when a
            release is selected. Admissions are retained after cancellation or a
            replacement; a submitted download is never cancelled by a quota
            change. Administrators and accounts with “Bypass request quotas” are
            exempt.
          </p>
        </details>
        <div className="button-row">
          <button
            disabled={
              busy || policies.isPending || !!policies.error || duplicate
            }
          >
            {save.isPending ? "Saving…" : "Save limits"}
          </button>
          <button
            type="button"
            disabled={busy || policies.isPending || !stored}
            onClick={() => inherit.mutate()}
          >
            {scope === "installation"
              ? "Remove default limits"
              : "Restore inherited limits"}
          </button>
        </div>
        {message && <p role="status">{message}</p>}
      </form>
      <details className="policy-disclosure">
        <summary>Per-user usage</summary>
        <div className="policy-stack">
          {users.isPending && <Loading />}
          {users.data?.length === 0 && (
            <p className="policy-empty">No users on this page.</p>
          )}
          <div className="policy-table-scroll">
            <table className="policy-usage-table">
              <caption className="sr-only">Per-user request usage</caption>
              <thead>
                <tr>
                  <th scope="col">User</th>
                  <th scope="col">Pending</th>
                  <th scope="col">Rolling usage</th>
                </tr>
              </thead>
              <tbody>
                {users.data?.map((user) => (
                  <tr key={user.user_id}>
                    <td>{user.user_name}</td>
                    <td>{user.pending}</td>
                    <td>
                      {user.bypass ? (
                        "Bypass enabled"
                      ) : user.windows.length ? (
                        <ul className="policy-usage-windows">
                          {user.windows.map((window) => (
                            <li key={`${window.medium}:${window.window}`}>
                              <strong>
                                {
                                  {
                                    combined: "All formats",
                                    ebook: "Ebooks",
                                    audio: "Audiobooks",
                                  }[window.medium]
                                }{" "}
                                ·{" "}
                                {
                                  {
                                    day: "Last 24 hours",
                                    week: "Last 7 days",
                                    month: "Last 30 days",
                                  }[window.window]
                                }
                              </strong>
                              <span>
                                {window.used_books} /{" "}
                                {window.books ?? "unlimited"} books
                              </span>
                              <span>
                                {(window.used_bytes / 1024 ** 3).toFixed(2)} /{" "}
                                {window.size_bytes == null
                                  ? "unlimited"
                                  : (window.size_bytes / 1024 ** 3).toFixed(
                                      2,
                                    )}{" "}
                                GiB
                              </span>
                            </li>
                          ))}
                        </ul>
                      ) : (
                        "Unlimited"
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="button-row">
            <button
              disabled={busy || users.isFetching || !offset}
              onClick={() => {
                setOffset(Math.max(0, offset - 50));
              }}
            >
              Previous users
            </button>
            <button
              disabled={
                busy || users.isFetching || (users.data?.length ?? 0) < 50
              }
              onClick={() => {
                setOffset(offset + 50);
              }}
            >
              More users
            </button>
          </div>
        </div>
      </details>
    </section>
  );
}
