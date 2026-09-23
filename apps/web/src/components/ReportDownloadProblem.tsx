import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Notice } from "../components";

type Reason = components["schemas"]["ReportInput"]["reason"];

export default function ReportDownloadProblem({
  selectionId,
  assetId,
}: {
  selectionId: string;
  assetId?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState<Reason>("wrong-book");
  const [approval, setApproval] = useState(false);
  const cache = useQueryClient();
  const report = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/acquisition/recovery/reports", {
          body: {
            selection_id: selectionId,
            asset_id: assetId,
            reason,
            require_approval: approval,
          },
        }),
      ),
    onSuccess: async () => {
      setOpen(false);
      await Promise.all(
        [
          "downloads",
          "book-downloads",
          "activity",
          "requests",
          "release-blocklist",
        ].map((key) => cache.invalidateQueries({ queryKey: [key] })),
      );
    },
  });
  return (
    <div>
      <button onClick={() => setOpen(!open)} aria-expanded={open}>
        Report a problem
      </button>
      <Notice error={report.error} />
      {report.isSuccess && (
        <p role="status">
          Problem reported. Follow the replacement in download activity.
        </p>
      )}
      {open && (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            report.mutate();
          }}
        >
          <label>
            Problem with this release
            <select
              value={reason}
              onChange={(event) => setReason(event.target.value as Reason)}
            >
              <option value="wrong-book">Wrong book</option>
              <option value="wrong-language">Wrong language</option>
              <option value="bad-audio">Bad audio</option>
              <option value="missing-chapters">Missing chapters</option>
              <option value="wrong-narrator">Wrong narrator</option>
              <option value="drm">DRM</option>
              <option value="incomplete">Incomplete content</option>
            </select>
          </label>
          <p>
            This blocks the release and searches for a replacement using your
            original requirements. For a shared pack, each requested book gets a
            replacement. Existing files are preserved.
          </p>
          <label>
            <input
              type="checkbox"
              checked={approval}
              onChange={(event) => setApproval(event.target.checked)}
            />
            Wait for administrator approval before replacement
          </label>
          <button disabled={report.isPending}>
            Report and request replacement
          </button>
        </form>
      )}
    </div>
  );
}
