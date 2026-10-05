import { useQuery } from "@tanstack/react-query";
import { TriangleAlert } from "lucide-react";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import "./connection-health.css";

export default function ConnectionHealth() {
  const health = useQuery({
    queryKey: ["connection-health"],
    queryFn: async () => {
      const snapshot = result(await api.GET("/api/health/connections"));
      if (!snapshot.issues) return snapshot;
      // Verify saved failures with the Settings checks before publishing an alert.
      // The server coalesces requests and enforces a recovery-check cooldown.
      return result(await api.POST("/api/health/connections/recheck"));
    },
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: 1,
  });
  const issues =
    health.data?.connections?.filter(
      (connection) => connection.status !== "connected",
    ) ?? [];
  if (!health.isError && (health.data?.rechecking || issues.length === 0))
    return null;
  const label = health.isError
    ? "Connection status unavailable"
    : `${issues.length} connection ${issues.length === 1 ? "issue" : "issues"}`;
  return (
    <details className="connection-health">
      <summary
        className="topbar-action connection-health-trigger"
        aria-label={label}
      >
        <TriangleAlert size={18} aria-hidden="true" />
        <span aria-live="polite">{label}</span>
      </summary>
      <div className="connection-health-panel" aria-label="Connection issues">
        <strong>Connections need attention</strong>
        {health.isError && (
          <p role="status">
            Unable to refresh connection health. Check your connection to
            Dewarr.
          </p>
        )}
        <ul>
          {issues.map((issue) => (
            <li key={issue.key}>
              <strong>{issue.name}</strong>
              <p>{issue.message}</p>
              <small>
                {issue.checked_at
                  ? `Last checked ${new Date(issue.checked_at).toLocaleString()}`
                  : "Not checked yet"}
              </small>
              {issue.settings_url ? (
                <Link
                  to={issue.settings_url}
                  onClick={(event) =>
                    event.currentTarget
                      .closest("details")
                      ?.removeAttribute("open")
                  }
                >
                  Open settings
                </Link>
              ) : (
                <small>Ask an administrator to check this connection.</small>
              )}
            </li>
          ))}
        </ul>
        <small>
          Connection issues are automatically rechecked before this alert
          appears and retried about once a minute while you’re using Dewarr.
          Routine checks run every 5 minutes. Service cooldowns may delay a
          retry.
        </small>
      </div>
    </details>
  );
}
