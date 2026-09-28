import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
import { randomUUID } from "../randomUUID";

// An empty folder list can mean inventory has not finished, even when a server
// is connected. Keep connection state separate from available folder choices.
export default function LibraryFolderRecovery({
  refresh,
  refreshing,
}: {
  refresh: () => void;
  refreshing: boolean;
}) {
  const cache = useQueryClient();
  const connections = useQuery({
    queryKey: ["connections"],
    queryFn: async () => result(await api.GET("/api/integrations")),
  });
  const sync = useMutation({
    mutationFn: async (id: string) =>
      result(
        await api.POST("/api/integrations/{integration_id}/sync", {
          params: {
            path: { integration_id: id },
            header: { "idempotency-key": randomUUID() },
          },
        }),
      ),
    onSuccess: (operation, integrationId) =>
      cache.invalidateQueries({
        queryKey: ["library-folder-sync", integrationId, operation.id],
      }),
  });
  const operation = useQuery({
    queryKey: ["library-folder-sync", sync.variables, sync.data?.id],
    enabled: !!sync.data,
    queryFn: async () =>
      result(
        await api.GET(
          "/api/integrations/{integration_id}/sync/{operation_id}",
          {
            params: {
              path: {
                integration_id: sync.variables!,
                operation_id: sync.data!.id,
              },
            },
          },
        ),
      ),
    retry: false,
    refetchInterval: (query) =>
      query.state.status === "error" ||
      (query.state.data &&
        !["queued", "running"].includes(query.state.data.status))
        ? false
        : 2000,
  });
  const status = operation.data?.status || sync.data?.status;
  const busy =
    sync.isPending ||
    (!operation.isError && (status === "queued" || status === "running"));
  useEffect(() => {
    if (status === "completed") {
      void Promise.all([
        cache.invalidateQueries({ queryKey: ["library-folder-options"] }),
        cache.invalidateQueries({ queryKey: ["library-folder-settings"] }),
        cache.invalidateQueries({ queryKey: ["connections"] }),
      ]);
    }
  }, [cache, status, sync.data?.id]);

  return (
    <div className="notice">
      <Notice error={connections.error || sync.error || operation.error} />
      {connections.isPending ? (
        <Loading />
      ) : connections.data?.length ? (
        <>
          <p>
            No compatible library folders are available. Sync your library
            server, then check its folder settings and account permissions if
            folders are still missing.
          </p>
          {connections.data.map((connection) => (
            <div key={connection.id}>
              <p>
                <strong>{connection.name}</strong>
                {connection.enabled
                  ? connection.last_error
                    ? `: ${connection.last_error}`
                    : " · Sync to refresh available libraries."
                  : " · Enable this connection in Libraries settings."}
              </p>
              {connection.enabled && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => sync.mutate(connection.id)}
                >
                  Sync {connection.name}
                </button>
              )}
            </div>
          ))}
        </>
      ) : connections.data ? (
        <p>Connect Audiobookshelf or Grimmory to choose a library folder.</p>
      ) : null}
      {sync.data && (
        <p role="status">
          {operation.isError
            ? "Could not check sync progress. Refresh libraries or try syncing again."
            : operation.data?.message || sync.data.message}{" "}
          <Link to="/activity">View Activity</Link>
        </p>
      )}
      <button
        type="button"
        disabled={refreshing}
        onClick={() => {
          void connections.refetch();
          if (sync.data) void operation.refetch();
          refresh();
        }}
      >
        Refresh libraries
      </button>
    </div>
  );
}
