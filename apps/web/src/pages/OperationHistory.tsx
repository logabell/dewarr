import { Fragment, useEffect, useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import { ChevronRight } from "lucide-react";
import { api, result } from "../api/client";
import { Empty, Loading, Notice } from "../components";

const PAGE_SIZE = 50;

const labels: Record<string, string> = {
  "acquisition.auto-select": "Automatic release preparation",
  "lists.requests": "List wanted media",
  "lists.csv": "CSV list import",
  "lists.sync": "External list observation",
  "lists.curate": "List curation",
  "lists.writeback": "Hardcover list write-back",
  "lists.writeback.compare": "Hardcover list comparison",
  "discovery.follow-list": "Follow community list",
  "sources.search": "Book source search",
  "system.probe": "Background worker check",
  "organization.automatic": "Automatic library import",
  "library.sync": "Audiobookshelf inventory sync",
  "acquisition.evaluate": "Wanted media check",
  "acquisition.download": "Book download",
  "acquisition.repair": "Download connection repair",
  "acquisition.review": "Download import review",
  "acquisition.select": "Release selection",
  "metadata.enrich": "Automatic metadata lookup",
  "library.match": "Library matching",
  "library.combine": "Combining multi-part books",
  "metadata.resolve-import": "Imported metadata lookup",
};

export default function OperationHistory({ actions }: { actions?: ReactNode }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const location = useLocation();
  const q = (params.get("q") || "").slice(0, 300);
  const status = (params.get("status") || "").slice(0, 40);
  const kind = (params.get("kind") || "").slice(0, 60);
  const rawOffset = Number(params.get("offset") || 0);
  const offset =
    Number.isSafeInteger(rawOffset) && rawOffset >= 0
      ? Math.floor(rawOffset / PAGE_SIZE) * PAGE_SIZE
      : 0;
  const cache = useQueryClient();
  function change(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "offset") next.delete("offset");
    navigate({
      pathname: location.pathname,
      search: next.toString(),
      hash: location.hash,
    });
  }
  const activity = useQuery({
    queryKey: ["activity", "history", q, status, kind, offset],
    queryFn: async ({ signal }) =>
      result(
        await api.GET("/api/activity/page", {
          signal,
          params: { query: { q, status, kind, offset, limit: PAGE_SIZE } },
        }),
      ),
    refetchInterval: (query) =>
      query.state.data?.items.some((item) =>
        ["queued", "running", "retrying"].includes(item.status),
      )
        ? 2000
        : 15000,
    staleTime: 0,
    gcTime: 0,
    retry: false,
  });
  useEffect(() => {
    if (
      !activity.error &&
      activity.data?.items.some(
        (item) => item.kind === "library.sync" && item.status === "completed",
      )
    ) {
      for (const key of [
        "assets",
        "catalog",
        "work",
        "works",
        "requests",
        "request-preview",
        "libraries",
        "connections",
      ]) {
        void cache.invalidateQueries({ queryKey: [key] });
      }
    }
  }, [activity.data, activity.error, cache]);
  const filtered = Boolean(q || status || kind);
  const kinds = Array.from(
    new Set([
      ...(activity.error ? [] : activity.data?.kinds || []),
      ...(kind ? [kind] : []),
    ]),
  ).sort();
  const statuses = Array.from(
    new Set([
      ...(activity.error ? [] : activity.data?.statuses || []),
      ...(status ? [status] : []),
    ]),
  ).sort();
  return (
    <section
      className="operation-history"
      aria-labelledby="operation-history-title"
    >
      <div className="logs-heading">
        <div>
          <h2 id="operation-history-title">Background activity</h2>
          <p className="muted">Jobs and library checks, newest first.</p>
        </div>
        {actions}
      </div>
      <form
        className="logs-toolbar"
        aria-label="Search background activity"
        onSubmit={(event) => {
          event.preventDefault();
          change(
            "q",
            String(new FormData(event.currentTarget).get("q") || "").trim(),
          );
        }}
      >
        <label>
          <span className="sr-only">Search activity</span>
          <input
            key={q}
            name="q"
            type="search"
            maxLength={300}
            defaultValue={q}
            placeholder="Search logs…"
          />
        </label>
        <button type="submit" aria-label="Search activity">
          Search
        </button>
        <label>
          <span className="sr-only">Activity status</span>
          <select
            value={status}
            onChange={(event) => change("status", event.target.value)}
          >
            <option value="">All statuses</option>
            {statuses.map((value) => (
              <option key={value} value={value}>
                {value.charAt(0).toUpperCase() + value.slice(1)}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="sr-only">Task type</span>
          <select
            value={kind}
            onChange={(event) => change("kind", event.target.value)}
          >
            <option value="">All task types</option>
            {kinds.map((value) => (
              <option key={value} value={value}>
                {labels[value] || value}
              </option>
            ))}
          </select>
        </label>
        {(filtered || offset > 0) && (
          <button
            type="button"
            aria-label="Reset activity view"
            onClick={() => {
              const next = new URLSearchParams(params);
              for (const key of ["q", "status", "kind", "offset"])
                next.delete(key);
              navigate({
                pathname: location.pathname,
                search: next.toString(),
                hash: location.hash,
              });
            }}
          >
            Clear
          </button>
        )}
      </form>
      <Notice error={activity.error} />
      {activity.isPending && <Loading />}
      {activity.error && (
        <button
          disabled={activity.isFetching}
          onClick={() => activity.refetch()}
        >
          Retry activity
        </button>
      )}
      {activity.data && !activity.error && (
        <>
          <p className="logs-count muted" role="status">
            {activity.data.total} matching{" "}
            {activity.data.total === 1 ? "operation" : "operations"}
          </p>
          {activity.data.items.length ? (
            <div
              className="logs-table-scroll"
              role="region"
              aria-label="Log entries"
              tabIndex={0}
            >
              <table className="logs-table">
                <caption className="sr-only">
                  Background activity, newest first
                </caption>
                <thead>
                  <tr>
                    <th scope="col" aria-label="Details" />
                    <th scope="col">Time</th>
                    <th scope="col">Status</th>
                    <th scope="col">Task</th>
                    <th scope="col">Message</th>
                  </tr>
                </thead>
                <tbody>
                  {activity.data.items.map((item) => (
                    <Fragment key={item.id}>
                      <tr className="logs-entry">
                        <td>
                          <button
                            className="logs-expand"
                            aria-label={`Operation details: ${labels[item.kind] || item.kind}`}
                            aria-expanded={expanded === item.id}
                            aria-controls={`log-${item.id}`}
                            onClick={() =>
                              setExpanded(expanded === item.id ? null : item.id)
                            }
                          >
                            <ChevronRight size={14} aria-hidden="true" />
                          </button>
                        </td>
                        <td className="logs-time">
                          <time
                            dateTime={item.created_at}
                            title={new Date(item.created_at).toLocaleString()}
                          >
                            {new Date(item.created_at).toLocaleDateString(
                              undefined,
                              { month: "short", day: "numeric" },
                            )}{" "}
                            <span>
                              {new Date(item.created_at).toLocaleTimeString(
                                undefined,
                                {
                                  hour: "2-digit",
                                  minute: "2-digit",
                                  second: "2-digit",
                                  hour12: false,
                                },
                              )}
                            </span>
                          </time>
                        </td>
                        <td>
                          <span
                            className="logs-status"
                            data-status={item.status}
                          >
                            {item.status}
                          </span>
                        </td>
                        <td
                          className="logs-task"
                          title={labels[item.kind] || item.kind}
                        >
                          {labels[item.kind] || item.kind}
                        </td>
                        <td
                          className="logs-message"
                          title={item.message || undefined}
                        >
                          {item.message || "—"}
                        </td>
                      </tr>
                      <tr
                        id={`log-${item.id}`}
                        hidden={expanded !== item.id}
                        className="logs-detail-row"
                      >
                        <td colSpan={5}>
                          <div className="logs-detail">
                            <p>{item.message}</p>
                            {item.context && (
                              <Link to={item.context.href}>
                                {item.context.label}
                              </Link>
                            )}
                            <p>
                              Operation ID: <code>{item.id}</code>
                            </p>
                            <p>Task type: {item.kind}</p>
                            <p>
                              Created:{" "}
                              <time dateTime={item.created_at}>
                                {new Date(item.created_at).toLocaleString()}
                              </time>
                            </p>
                            <p>
                              Last updated:{" "}
                              <time dateTime={item.updated_at}>
                                {new Date(item.updated_at).toLocaleString()}
                              </time>
                            </p>
                          </div>
                        </td>
                      </tr>
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <Empty
              title={
                filtered ? "No matching activity" : "No background activity"
              }
            >
              {filtered
                ? "Try another search or reset the filters."
                : "Your requests and background checks will appear here."}
            </Empty>
          )}
          <nav className="logs-pagination" aria-label="Log pages">
            <span className="muted">
              Page {Math.floor(offset / PAGE_SIZE) + 1} · 50 per page
            </span>
            <div>
              <button
                type="button"
                disabled={offset === 0}
                onClick={() =>
                  change("offset", String(Math.max(0, offset - PAGE_SIZE)))
                }
              >
                Previous
              </button>
              <button
                type="button"
                disabled={offset + PAGE_SIZE >= activity.data.total}
                onClick={() => change("offset", String(offset + PAGE_SIZE))}
              >
                Next
              </button>
            </div>
          </nav>
        </>
      )}
    </section>
  );
}
