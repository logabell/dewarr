import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
import ReportDownloadProblem from "./ReportDownloadProblem";

export default function DownloadRecoveryDetails({
  attemptId,
  workId,
}: {
  attemptId: string;
  workId: string;
}) {
  const [open, setOpen] = useState(false);
  const cache = useQueryClient();
  const query = useQuery({
    queryKey: ["downloads", "recovery", attemptId, workId],
    enabled: open,
    queryFn: async ({ signal }) =>
      result(
        await api.GET("/api/acquisition/downloads/{attempt_id}", {
          signal,
          params: {
            path: { attempt_id: attemptId },
            query: { work_id: workId },
          },
        }),
      ),
    refetchInterval: open ? 15000 : false,
  });
  const approve = useMutation({
    mutationFn: async (recoveryId: string) =>
      result(
        await api.POST("/api/acquisition/recovery/{recovery_id}/approve", {
          params: { path: { recovery_id: recoveryId } },
        }),
      ),
    onSuccess: async () => {
      await Promise.all(
        ["downloads", "book-downloads", "requests"].map((key) =>
          cache.invalidateQueries({ queryKey: [key] }),
        ),
      );
    },
  });
  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>Download history and recovery</summary>
      {open && (
        <div>
          <Notice error={query.error || approve.error} />
          {query.isPending && <Loading />}
          {query.error && (
            <button onClick={() => query.refetch()}>
              Retry download history
            </button>
          )}
          {query.data && (
            <>
              <ol aria-label="Download attempt chain">
                {query.data.attempt_chain?.map((step, index) => (
                  <li key={step.attempt_id}>
                    Attempt {index + 1}: {step.release_title} · {step.state}
                    <p>{step.reason}</p>
                  </li>
                ))}
              </ol>
              {query.data.recoveries?.map((recovery) => (
                <div key={recovery.id}>
                  <p>{recovery.message}</p>
                  <p className="muted">Transfer cleanup: {recovery.cleanup}</p>
                  {recovery.can_approve && (
                    <button
                      disabled={approve.isPending}
                      onClick={() => approve.mutate(recovery.id)}
                    >
                      Approve replacement
                    </button>
                  )}
                </div>
              ))}
              {query.data.can_report_problem && (
                <ReportDownloadProblem
                  selectionId={query.data.selection_id}
                  assetId={query.data.imported_asset_id}
                />
              )}
            </>
          )}
        </div>
      )}
    </details>
  );
}
