import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Notice } from "../components";
import { randomUUID } from "../randomUUID";

type Plan = components["schemas"]["FrozenPlanView"];
type Medium = "ebook" | "audio";
const labels: Record<string, string> = {
  queued: "Queued",
  publishing: "Publishing",
  "awaiting-library": "Waiting for the library",
  confirmed: "Available",
  held: "Needs attention",
  skipped: "Already available",
  cancelling: "Stopping import",
  "cancel-held": "Cancellation needs attention",
  cancelled: "Stopped",
};

export default function ImportExecution({
  plan,
  compact = false,
}: {
  plan: Plan;
  compact?: boolean;
}) {
  const cache = useQueryClient();
  const [choices, setChoices] = useState<Partial<Record<Medium, string>>>({});
  const attempt = useRef<{ payload: string; key: string } | null>(null);
  const queryKey = ["import-execution", plan.id];
  const refresh = () =>
    Promise.all([
      cache.invalidateQueries({ queryKey }),
      cache.invalidateQueries({ queryKey: ["inspection", plan.inspection_id] }),
      cache.invalidateQueries({ queryKey: ["requests"] }),
    ]);
  const query = useQuery({
    queryKey,
    refetchOnMount: "always",
    queryFn: async () => {
      const [destinations, runs] = await Promise.all([
        api.GET("/api/organization/destinations").then(result),
        api
          .GET("/api/organization/plans/{plan_id}/imports", {
            params: { path: { plan_id: plan.id } },
          })
          .then(result),
      ]);
      return { destinations, runs };
    },
    refetchInterval: (query) =>
      query.state.data?.runs.some((run) =>
        run.entries.some((entry) =>
          ["queued", "publishing", "awaiting-library", "cancelling"].includes(
            entry.state,
          ),
        ),
      )
        ? 2000
        : false,
  });
  const media = [
    ...new Set(
      plan.document.plan.items
        .filter((item) => item.state === "ready")
        .map((item) => item.medium as Medium),
    ),
  ];
  const eligible = (medium: Medium) =>
    query.data?.destinations.filter(
      (row) => row.medium === medium && row.publication_available,
    ) || [];
  const choice = (medium: Medium) =>
    eligible(medium).find(
      (row) =>
        row.id ===
        (choices[medium] ||
          plan.document.destinations?.[medium] ||
          (eligible(medium).length === 1 ? eligible(medium)[0].id : "")),
    );
  const publish = useMutation({
    mutationFn: async () => {
      const destinations = Object.fromEntries(
        media.map((medium) => [
          medium,
          { id: choice(medium)!.id, revision: choice(medium)!.revision },
        ]),
      );
      const body = { plan_revision: plan.revision, destinations };
      const payload = JSON.stringify(body);
      if (attempt.current?.payload !== payload)
        attempt.current = { payload, key: randomUUID() };
      return result(
        await api.POST("/api/organization/plans/{plan_id}/imports", {
          params: {
            path: { plan_id: plan.id },
            header: { "idempotency-key": attempt.current.key },
          },
          body,
        }),
      );
    },
    onSuccess: () => {
      attempt.current = null;
      return refresh();
    },
  });
  const retry = useMutation({
    mutationFn: async ({
      runId,
      entryId,
    }: {
      runId: string;
      entryId: string;
    }) =>
      result(
        await api.POST(
          "/api/organization/imports/{run_id}/entries/{entry_id}/retry",
          { params: { path: { run_id: runId, entry_id: entryId } } },
        ),
      ),
    onSuccess: refresh,
  });
  const executable =
    plan.document.profile.layout === "conventional" &&
    !!Object.keys(plan.document.version_revisions || {}).length;
  const cancel = useMutation({
    mutationFn: async ({
      runId,
      entryId,
    }: {
      runId: string;
      entryId: string;
    }) =>
      result(
        await api.POST(
          "/api/organization/imports/{run_id}/entries/{entry_id}/cancel",
          {
            params: { path: { run_id: runId, entry_id: entryId } },
          },
        ),
      ),
    onSuccess: refresh,
  });
  const hasRuns = !!query.data?.runs.some((run) => run.entries.length > 0);
  return (
    <section
      className={compact ? "import-execution-compact" : "library-access"}
      aria-label="Import books"
    >
      {!hasRuns && !query.isPending && (
        <>
          <h3>Import into your library</h3>
          <p className="muted">
            Resolved books import independently. Availability updates after
            Audiobookshelf confirms the item and files.
          </p>
          {media.map((medium) => (
            <label key={medium}>
              {medium === "ebook"
                ? "Ebook destination"
                : "Audiobook destination"}
              <select
                value={choice(medium)?.id || ""}
                disabled={publish.isPending}
                onChange={(event) =>
                  setChoices((current) => ({
                    ...current,
                    [medium]: event.target.value,
                  }))
                }
              >
                <option value="">Choose a verified destination</option>
                {eligible(medium).map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.root_key} · {row.mode === "copy" ? "Copy" : "Hardlink"}
                  </option>
                ))}
              </select>
            </label>
          ))}
          {!executable && (
            <p className="notice">
              Use a fresh conventional plan with frozen metadata and version
              evidence before publishing.
            </p>
          )}

          <button
            className="primary"
            disabled={
              !executable ||
              !media.length ||
              media.some((medium) => !choice(medium)) ||
              publish.isPending
            }
            onClick={() => publish.mutate()}
          >
            {publish.isPending ? "Queuing import…" : "Import resolved books"}
          </button>
        </>
      )}
      <Notice
        error={query.error || publish.error || retry.error || cancel.error}
      />
      {query.data?.runs.map((run) => (
        <section
          key={run.id}
          className="library-access"
          aria-label="Import result"
        >
          {run.entries.map((entry) => {
            const item = plan.document.plan.items.find(
              (item) => item.group_id === entry.group_id,
            );
            return (
              <div className={`import-result is-${entry.state}`} key={entry.id}>
                <strong>
                  {compact ? "" : `${item?.title || "Book"} · `}
                  {labels[entry.state] || entry.state}
                </strong>
                <p role="status">{entry.message}</p>
                {entry.cover_export && (
                  <p className="muted">{entry.cover_export.message}</p>
                )}
                {entry.state === "confirmed" && item && (
                  <Link to={`/books/${item.work_id}`}>View library book</Link>
                )}
                {entry.can_retry && (
                  <button
                    className="primary"
                    disabled={retry.isPending}
                    onClick={() =>
                      retry.mutate({ runId: run.id, entryId: entry.id })
                    }
                  >
                    {retry.isPending
                      ? "Retrying…"
                      : entry.published_at
                        ? "Retry library detection"
                        : "Retry import"}
                  </button>
                )}
                {entry.can_cancel && (
                  <details className="import-result-options">
                    <summary>More options</summary>
                    <button
                      disabled={cancel.isPending || retry.isPending}
                      onClick={() =>
                        cancel.mutate({ runId: run.id, entryId: entry.id })
                      }
                    >
                      {entry.state === "cancel-held"
                        ? "Retry stopping import"
                        : "Stop pending import"}
                    </button>
                    <p className="muted">
                      Downloaded files and already published books are
                      preserved.
                    </p>
                  </details>
                )}
                {entry.state === "cancelled" && (
                  <Link
                    to={`/organization/inspections?inspection=${plan.inspection_id}`}
                  >
                    Review files and create a new plan
                  </Link>
                )}
              </div>
            );
          })}
        </section>
      ))}
    </section>
  );
}
