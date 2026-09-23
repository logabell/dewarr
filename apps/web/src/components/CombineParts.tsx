import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Notice } from "../components";

type Status = components["schemas"]["CombineStatus"];

const STATES: Record<string, string> = {
  waiting: "Waiting for every part",
  ready: "Ready to combine",
  skipped: "Not combined",
  combining: "Combining",
  combined: "Combined into one book",
  separating: "Separating",
  separated: "Kept as separate parts",
  "needs-attention": "Needs attention",
};

export const combineStateLabel = (state?: string | null) =>
  (state && STATES[state]) || "Not combined";

export function CombineButton({
  libraryId,
  versionId,
  action = "combine",
}: {
  libraryId: string;
  versionId: string;
  action?: "combine" | "separate";
}) {
  const cache = useQueryClient();
  const run = useMutation({
    mutationFn: async () =>
      result(
        action === "combine"
          ? await api.POST("/api/library/versions/{version_id}/combine", {
              params: { path: { version_id: versionId } },
              body: { library_id: libraryId },
            })
          : await api.POST("/api/library/versions/{version_id}/separate", {
              params: { path: { version_id: versionId } },
              body: { library_id: libraryId },
            }),
      ),
    onSuccess: () => {
      cache.invalidateQueries({ queryKey: ["part-sets"] });
      cache.invalidateQueries({ queryKey: ["library-review"] });
    },
  });
  return (
    <>
      <button
        type="button"
        disabled={run.isPending || run.isSuccess}
        onClick={() => run.mutate()}
      >
        {run.isSuccess
          ? action === "combine"
            ? "Combining…"
            : "Separating…"
          : action === "combine"
            ? "Combine parts"
            : "Separate parts"}
      </button>
      <Notice error={run.error} />
    </>
  );
}

function PartSet({ status }: { status: Status }) {
  return (
    <li className="part-set">
      <div>
        <strong>{`${status.library_name}: ${status.total} parts`}</strong>
        <span className="status">{combineStateLabel(status.state)}</span>
      </div>
      {status.state !== "combined" && status.state !== "separating" && (
        <p className="muted">{`In this library: ${status.present.join(", ")}`}</p>
      )}
      {status.folder && (
        <p className="muted">
          Folder: <code>{status.folder}</code>
        </p>
      )}
      {status.reason && <p className="muted">{status.reason}</p>}
      {(status.can_combine || status.can_separate) && (
        <div className="button-row">
          {status.can_combine && (
            <CombineButton
              libraryId={status.library_id}
              versionId={status.version_id}
            />
          )}
          {status.can_separate && (
            <CombineButton
              libraryId={status.library_id}
              versionId={status.version_id}
              action="separate"
            />
          )}
        </div>
      )}
    </li>
  );
}

export default function PartSets({ workId }: { workId: string }) {
  const sets = useQuery({
    queryKey: ["part-sets", workId],
    queryFn: async ({ signal }) =>
      result(
        await api.GET("/api/library/works/{work_id}/part-sets", {
          signal,
          params: { path: { work_id: workId } },
        }),
      ),
    refetchInterval: (query) =>
      query.state.data?.some(
        (set) => set.state === "combining" || set.state === "separating",
      )
        ? 5000
        : false,
    retry: false,
  });
  if (!sets.data?.length) return <Notice error={sets.error} />;
  return (
    <section className="part-sets" aria-label="Books released in parts">
      <h3>Released in parts</h3>
      <p className="muted">
        Once every part is in an Audiobookshelf library, Dewarr combines them
        into one book with a Disc folder per part. It skips any part your
        connected Audiobookshelf account has started listening to.
      </p>
      <ul>
        {sets.data.map((status) => (
          <PartSet
            key={`${status.library_id}:${status.version_id}`}
            status={status}
          />
        ))}
      </ul>
    </section>
  );
}
