import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Notice } from "../components";

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
  const [draft, setDraft] = useState<Rules | null>(null);
  const [offset, setOffset] = useState(0);
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
      await cache.invalidateQueries({ queryKey: ["request-quotas"] });
    },
  });
  return (
    <section className="panel editor" aria-label="Request quotas">
      <h2>Request quotas</h2>
      <p>
        A user policy replaces their role policy, which replaces installation
        defaults. Blank limits are unlimited. Months are rolling 30-day windows.
        Each requested medium counts as one book. Existing library copies and
        shared transfers are exempt.
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
      <label>
        Apply limits to
        <select
          value={scope}
          onChange={(e) => {
            setScope(e.target.value);
            setDraft(null);
          }}
        >
          <option value="installation">Installation defaults</option>
          <option value="role:member">Default member role</option>
          {roles.data?.roles.map((role) => (
            <option key={role.id} value={`role:${role.id}`}>
              Role: {role.name}
            </option>
          ))}
          {users.data?.map((user) => (
            <option key={user.user_id} value={`user:${user.user_id}`}>
              User: {user.user_name}
            </option>
          ))}
        </select>
      </label>
      {!stored && scope !== "installation" && (
        <p>Currently inherits its limits. Saving creates an override.</p>
      )}
      {rules.windows.map((window, index) => (
        <fieldset key={index}>
          <legend>Rolling limit {index + 1}</legend>
          <label>
            Media
            <select
              value={window.medium}
              onChange={(e) =>
                setDraft({
                  ...rules,
                  windows: rules.windows.map((w, i) =>
                    i === index
                      ? { ...w, medium: e.target.value as typeof w.medium }
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
                      ? { ...w, window: e.target.value as typeof w.window }
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
              step="0.01"
              value={
                window.size_bytes == null ? "" : window.size_bytes / 1024 ** 3
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
                              : Math.round(Number(e.target.value) * 1024 ** 3),
                        }
                      : w,
                  ),
                })
              }
            />
          </label>
          <button
            type="button"
            onClick={() =>
              setDraft({
                ...rules,
                windows: rules.windows.filter((_, i) => i !== index),
              })
            }
          >
            Remove limit
          </button>
        </fieldset>
      ))}
      <button
        type="button"
        disabled={rules.windows.length >= 9}
        onClick={() =>
          setDraft({
            ...rules,
            windows: [
              ...rules.windows,
              {
                medium: "combined",
                window: "week",
                books: null,
                size_bytes: null,
              },
            ],
          })
        }
      >
        Add rolling limit
      </button>
      <label>
        Maximum pending approvals
        <input
          type="number"
          min="0"
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
            setDraft({ ...rules, exempt_admin_approved: e.target.checked })
          }
        />{" "}
        Exempt requests approved by an administrator
      </label>
      <p>
        Size is checked when a release is selected. Admissions are retained
        after cancellation or a replacement; a submitted download is never
        cancelled by a quota change. Administrators and accounts with “Bypass
        request quotas” are exempt.
      </p>
      <div className="button-row">
        <button
          disabled={save.isPending || policies.isPending}
          onClick={() => save.mutate()}
        >
          Save limits
        </button>
        <button
          disabled={inherit.isPending || !stored}
          onClick={() => inherit.mutate()}
        >
          Restore inherited limits
        </button>
      </div>
      <h3>Per-user usage</h3>
      <table>
        <thead>
          <tr>
            <th>User</th>
            <th>Pending</th>
            <th>Rolling usage</th>
          </tr>
        </thead>
        <tbody>
          {users.data?.map((user) => (
            <tr key={user.user_id}>
              <td>{user.user_name}</td>
              <td>{user.pending}</td>
              <td>
                {user.bypass
                  ? "Bypass enabled"
                  : user.windows
                      .map(
                        (w) =>
                          `${w.medium}: ${w.used_books}/${w.books ?? "unlimited"} books, ${(w.used_bytes / 1024 ** 3).toFixed(2)} GiB per ${w.window}`,
                      )
                      .join("; ") || "Unlimited"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="button-row">
        <button
          disabled={!offset}
          onClick={() => setOffset(Math.max(0, offset - 50))}
        >
          Previous users
        </button>
        <button
          disabled={(users.data?.length ?? 0) < 50}
          onClick={() => setOffset(offset + 50)}
        >
          More users
        </button>
      </div>
    </section>
  );
}
