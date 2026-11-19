import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, LockOpen } from "lucide-react";
import { api, result, type Auth } from "../api/client";
import type { components } from "../api/schema";

type Block = components["schemas"]["ReleaseBlockInfo"];

export function blockedSummary(block: Block) {
  const when = new Date(block.blocked_at).toLocaleDateString();
  return `${block.reason} (${block.automatic ? "found automatically" : "reported"} ${when})`;
}

// Why this release cannot be downloaded again, and the way out for administrators.
export default function ReleaseBlockNotice({ block }: { block: Block }) {
  const cache = useQueryClient();
  const { data: session } = useQuery<Auth | null>({
    queryKey: ["session"],
    enabled: false,
  });
  const admin = session?.user.role === "admin";
  const refresh = () => {
    for (const name of [
      "book-sources",
      "requests",
      "downloads",
      "activity",
      "release-blocklist",
    ])
      void cache.invalidateQueries({ queryKey: [name] });
  };
  const unblock = useMutation({
    mutationFn: async () =>
      result(
        await api.DELETE("/api/acquisition/recovery/blocklist/{block_id}", {
          params: { path: { block_id: block.id } },
        }),
      ),
    onSuccess: refresh,
  });
  const resume = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/acquisition/recovery/blocklist/{block_id}/resume",
          {
            params: { path: { block_id: block.id } },
          },
        ),
      ),
    onSuccess: refresh,
  });
  const busy = unblock.isPending || resume.isPending;
  const outcome = resume.data;
  const refused = outcome?.attempts.filter((attempt) => !attempt.resumed);
  const error = unblock.error || resume.error;
  return (
    <div
      className="release-block-notice"
      role="group"
      aria-label="Blocked release"
    >
      <p className="error">
        <CircleAlert size={14} aria-hidden /> Blocked: {blockedSummary(block)}
      </p>
      {admin && (
        <div className="button-row">
          {block.can_resume && (
            <button
              disabled={busy}
              onClick={() => resume.mutate()}
              title="Remove the block and continue the download that is already on your downloader"
            >
              <LockOpen size={14} aria-hidden />{" "}
              {resume.isPending ? "Resuming…" : "Unblock and resume"}
            </button>
          )}
          <button
            disabled={busy}
            onClick={() => unblock.mutate()}
            title="Allow this release to be selected again"
          >
            {unblock.isPending ? "Unblocking…" : "Unblock"}
          </button>
        </div>
      )}
      {outcome && outcome.resumed > 0 && (
        <p role="status">
          Resumed. Dewarr is checking the existing download; no new torrent was
          added.
        </p>
      )}
      {refused?.map((attempt) => (
        <p key={attempt.attempt_id} role="status" className="muted">
          Unblocked, but the existing download was not resumed:{" "}
          {attempt.message}.
        </p>
      ))}
      {error && (
        <p className="error" role="alert">
          {error.message}
        </p>
      )}
    </div>
  );
}
