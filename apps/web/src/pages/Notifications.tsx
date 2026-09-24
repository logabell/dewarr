import { useState } from "react";
import "./notifications.css";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";

type Channel = components["schemas"]["NotificationChannelView"];
type Input = components["schemas"]["NotificationChannelInput"];
const empty: Input = {
  name: "",
  kind: "webhook",
  installation: false,
  events: [],
  enabled: true,
  digest_minutes: 15,
};

export default function Notifications({ admin }: { admin: boolean }) {
  const cache = useQueryClient();
  const [editing, setEditing] = useState<Channel | null>(null);
  const [form, setForm] = useState<Input>(empty);
  const [destination, setDestination] = useState("");
  const [token, setToken] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [historyId, setHistoryId] = useState<string | null>(null);
  const [notice, setNotice] = useState("");
  const settings = useQuery({
    queryKey: ["notifications"],
    queryFn: async () => result(await api.GET("/api/notifications")),
    refetchInterval: 5000,
  });
  const history = useQuery({
    queryKey: ["notification-history", historyId],
    enabled: !!historyId,
    queryFn: async () =>
      result(
        await api.GET("/api/notifications/channels/{channel_id}/deliveries", {
          params: { path: { channel_id: historyId! } },
        }),
      ),
    refetchInterval: historyId ? 5000 : false,
  });
  const refresh = async () => {
    await Promise.all([
      cache.invalidateQueries({ queryKey: ["notifications"] }),
      cache.invalidateQueries({ queryKey: ["notification-history"] }),
    ]);
  };
  const save = useMutation({
    mutationFn: async () => {
      const body: Input = {
        ...form,
        secrets: destination
          ? {
              url: form.kind === "apprise" ? "" : destination.trim(),
              urls:
                form.kind === "apprise"
                  ? destination
                      .split("\n")
                      .map((url) => url.trim())
                      .filter(Boolean)
                  : [],
              token,
            }
          : null,
      };
      return result(
        editing
          ? await api.PUT("/api/notifications/channels/{channel_id}", {
              params: { path: { channel_id: editing.id } },
              body,
            })
          : await api.POST("/api/notifications/channels", { body }),
      );
    },
    onSuccess: async () => {
      setShowForm(false);
      setDestination("");
      setToken("");
      setNotice("Channel saved.");
      await refresh();
    },
  });
  const action = useMutation({
    mutationFn: async ({
      channel,
      type,
    }: {
      channel: Channel;
      type: "test" | "delete" | "toggle";
    }) => {
      const params = { path: { channel_id: channel.id } };
      if (type === "test")
        return result(
          await api.POST("/api/notifications/channels/{channel_id}/test", {
            params,
          }),
        );
      if (type === "delete")
        return result(
          await api.DELETE("/api/notifications/channels/{channel_id}", {
            params,
          }),
        );
      return result(
        await api.PUT("/api/notifications/channels/{channel_id}", {
          params,
          body: {
            name: channel.name,
            kind: channel.kind as Input["kind"],
            installation: channel.installation,
            events: channel.events.filter((event) =>
              settings.data?.allowed_events.includes(event),
            ),
            enabled: !channel.enabled,
            digest_minutes: channel.digest_minutes as Input["digest_minutes"],
          },
        }),
      );
    },
    onSuccess: async (_, variables) => {
      setNotice(
        variables.type === "test"
          ? "Test queued. Delivery status will update after the worker runs (within a minute)."
          : "Channel updated.",
      );
      await refresh();
    },
  });
  const policy = useMutation({
    mutationFn: async (member_events: string[]) =>
      result(
        await api.PUT("/api/notifications/policy", { body: { member_events } }),
      ),
    onSuccess: refresh,
  });
  const labels = settings.data?.event_labels ?? {};
  const choices = (settings.data?.allowed_events ?? []).filter(
    (event) => !form.installation || !event.startsWith("discovery."),
  );
  function edit(channel: Channel | null) {
    setEditing(channel);
    setForm(
      channel
        ? {
            name: channel.name,
            kind: channel.kind as Input["kind"],
            installation: channel.installation,
            events: channel.events.filter((event) =>
              settings.data?.allowed_events.includes(event),
            ),
            enabled: channel.enabled,
            digest_minutes: channel.digest_minutes as Input["digest_minutes"],
          }
        : {
            ...empty,
            events:
              settings.data?.allowed_events.filter(
                (event) => event !== "request.pending",
              ) ?? [],
          },
    );
    setDestination("");
    setToken("");
    setShowForm(true);
    setNotice("");
  }
  return (
    <section className="notification-settings" aria-label="Notifications">
      <p className="muted">
        Send request updates and review alerts to your preferred service.
        Personal channels only receive your activity; approvers can also receive
        requests waiting for approval.
      </p>
      <Notice
        error={
          settings.error ||
          save.error ||
          action.error ||
          policy.error ||
          history.error
        }
      />
      {notice && <p role="status">{notice}</p>}
      {settings.isPending && <Loading />}
      {settings.data && (
        <>
          {!showForm && (
            <button className="primary" onClick={() => edit(null)}>
              Add channel
            </button>
          )}
          {showForm && (
            <form
              className="settings-form"
              onSubmit={(event) => {
                event.preventDefault();
                save.mutate();
              }}
            >
              <h4>{editing ? "Edit channel" : "New channel"}</h4>
              <label>
                Name
                <input
                  required
                  maxLength={120}
                  value={form.name}
                  onChange={(event) =>
                    setForm({ ...form, name: event.target.value })
                  }
                />
              </label>
              <label>
                Service
                <select
                  value={form.kind}
                  onChange={(event) => {
                    setForm({
                      ...form,
                      kind: event.target.value as Input["kind"],
                    });
                    setDestination("");
                  }}
                >
                  <option value="webhook">JSON webhook</option>
                  <option value="discord">Discord</option>
                  <option value="ntfy">ntfy</option>
                  <option value="apprise">Apprise</option>
                </select>
              </label>
              {admin && (
                <label>
                  Audience
                  <select
                    disabled={!!editing}
                    value={form.installation ? "installation" : "personal"}
                    onChange={(event) =>
                      setForm({
                        ...form,
                        installation: event.target.value === "installation",
                        events:
                          event.target.value === "installation"
                            ? form.events.filter(
                                (name) => !name.startsWith("discovery."),
                              )
                            : form.events,
                      })
                    }
                  >
                    <option value="personal">Personal</option>
                    <option value="installation">
                      Installation (shared request and connection alerts)
                    </option>
                  </select>
                </label>
              )}
              <label>
                {form.kind === "apprise"
                  ? "Apprise URLs (one per line)"
                  : form.kind === "ntfy"
                    ? "ntfy topic URL"
                    : "Webhook URL"}
                <textarea
                  required={!editing || form.kind !== editing.kind}
                  autoComplete="off"
                  spellCheck={false}
                  value={destination}
                  onChange={(event) => setDestination(event.target.value)}
                  placeholder={
                    editing
                      ? "Leave blank to keep saved credentials"
                      : form.kind === "ntfy"
                        ? "https://ntfy.sh/your-topic"
                        : "Enter destination"
                  }
                />
              </label>
              {form.kind !== "apprise" && form.kind !== "discord" && (
                <label>
                  Bearer token (optional)
                  <input
                    type="password"
                    autoComplete="new-password"
                    value={token}
                    onChange={(event) => setToken(event.target.value)}
                  />
                </label>
              )}
              <p className="muted">
                Destinations and tokens are encrypted and are never displayed
                after saving. To replace a token, enter the destination again.
              </p>
              <fieldset>
                <legend>Events</legend>
                {choices.map((name) => (
                  <label key={name}>
                    <input
                      type="checkbox"
                      checked={form.events.includes(name)}
                      onChange={(event) =>
                        setForm({
                          ...form,
                          events: event.target.checked
                            ? [...form.events, name]
                            : form.events.filter((value) => value !== name),
                        })
                      }
                    />
                    {labels[name]}
                  </label>
                ))}
              </fieldset>
              <label>
                Discovery digest
                <select
                  value={form.digest_minutes}
                  onChange={(event) =>
                    setForm({
                      ...form,
                      digest_minutes: Number(
                        event.target.value,
                      ) as Input["digest_minutes"],
                    })
                  }
                >
                  <option value={0}>Send immediately</option>
                  <option value={5}>Every 5 minutes</option>
                  <option value={15}>Every 15 minutes</option>
                  <option value={60}>Hourly</option>
                  <option value={1440}>Daily</option>
                </select>
              </label>
              <p className="muted">
                Digest applies to list, author, series and gap discoveries.
                Request and review alerts send immediately.
              </p>
              <div className="actions">
                <button className="primary" disabled={save.isPending}>
                  Save channel
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setShowForm(false);
                    setDestination("");
                    setToken("");
                  }}
                >
                  Cancel
                </button>
              </div>
            </form>
          )}
          {!settings.data.channels.length && !showForm && (
            <p>No notification channels yet.</p>
          )}
          {settings.data.channels.map((channel) => (
            <article className="panel" key={channel.id}>
              <h4>{channel.name}</h4>
              <p>
                {channel.kind} ·{" "}
                {channel.installation ? "Installation" : "Personal"} ·{" "}
                {channel.enabled ? "Enabled" : "Paused"}
              </p>
              <p>
                Last delivery:{" "}
                <strong>{channel.last_status ?? "Not tested"}</strong>
                {channel.last_delivery_at &&
                  ` · ${new Date(channel.last_delivery_at).toLocaleString()}`}
              </p>
              {channel.last_message && <p>{channel.last_message}</p>}
              <div className="actions">
                <button onClick={() => edit(channel)}>Edit</button>
                <button
                  disabled={action.isPending || !channel.enabled}
                  onClick={() => action.mutate({ channel, type: "test" })}
                >
                  Send test
                </button>
                <button
                  disabled={action.isPending}
                  onClick={() => action.mutate({ channel, type: "toggle" })}
                >
                  {channel.enabled ? "Pause" : "Enable"}
                </button>
                <button
                  onClick={() =>
                    setHistoryId(historyId === channel.id ? null : channel.id)
                  }
                >
                  Delivery history
                </button>
                <button
                  disabled={action.isPending}
                  onClick={() => action.mutate({ channel, type: "delete" })}
                >
                  Delete
                </button>
              </div>
              {historyId === channel.id && (
                <ul>
                  {history.data?.map((delivery) => (
                    <li key={delivery.id}>
                      {labels[delivery.event_type] ?? "Test"} — {delivery.state}
                      {delivery.message ? `: ${delivery.message}` : ""}
                    </li>
                  ))}
                  {history.data?.length === 0 && <li>No deliveries yet.</li>}
                </ul>
              )}
            </article>
          ))}
          {admin && (
            <details>
              <summary>Events members may configure</summary>
              <p>
                Changes also apply to queued deliveries. Approval alerts
                additionally require permission to manage requests.
              </p>
              <fieldset>
                <legend>Allowed member events</legend>
                {Object.entries(labels).map(([name, label]) => (
                  <label key={name}>
                    <input
                      type="checkbox"
                      disabled={policy.isPending}
                      checked={settings.data.member_events.includes(name)}
                      onChange={(event) =>
                        policy.mutate(
                          event.target.checked
                            ? [...settings.data.member_events, name]
                            : settings.data.member_events.filter(
                                (value) => value !== name,
                              ),
                        )
                      }
                    />
                    {label}
                  </label>
                ))}
              </fieldset>
            </details>
          )}
        </>
      )}
    </section>
  );
}
