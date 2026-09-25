import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, CircleCheck, Download, LoaderCircle } from "lucide-react";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { randomUUID } from "../randomUUID";

type SavedDownload = components["schemas"]["ReleaseDownloadStatus"];
const labels: Record<SavedDownload["state"], string> = {
  preparing: "Starting download",
  queued: "Download queued",
  downloading: "Downloading",
  downloaded: "Downloaded · awaiting import",
  imported: "Downloaded and imported",
  failed: "Download not started",
  cancelled: "Download cancelled",
  selected: "Release prepared",
  "needs-review": "Download needs attention",
};

export default function SourceReleaseDownload({
  searchId,
  resultId,
  title,
  disabled,
  disabledReason,
  download,
  offerWedge = false,
  showLabel = false,
}: {
  searchId: string;
  resultId: string;
  title: string;
  disabled: boolean;
  disabledReason?: string;
  download?: SavedDownload | null;
  offerWedge?: boolean;
  showLabel?: boolean;
}) {
  const key = useRef(randomUUID());
  const cache = useQueryClient();
  const [operationId, setOperationId] = useState<string>();
  const [useWedge, setUseWedge] = useState(false);
  const start = useMutation({
    mutationFn: async (spendWedge: boolean) =>
      result(
        await api.POST(
          "/api/source-searches/{search_id}/results/{result_id}/download",
          {
            params: {
              path: { search_id: searchId, result_id: resultId },
              header: { "idempotency-key": key.current },
              query: spendWedge ? { use_wedge: true } : {},
            },
          },
        ),
      ),
    onSuccess: (operation) => {
      setOperationId(operation.id);
      for (const name of ["requests", "book-sources"])
        void cache.invalidateQueries({ queryKey: [name] });
    },
  });
  const status = useQuery({
    queryKey: ["source-release-download", operationId],
    enabled: !!operationId,
    queryFn: async () =>
      result(
        await api.GET("/api/acquisition/automatic-selections/{operation_id}", {
          params: { path: { operation_id: operationId! } },
        }),
      ),
    refetchInterval: (query) =>
      query.state.data &&
      !["queued", "running"].includes(query.state.data.status)
        ? false
        : 1500,
  });
  useEffect(() => {
    if (status.data && !["queued", "running"].includes(status.data.status)) {
      for (const name of ["requests", "downloads", "activity", "book-sources"])
        void cache.invalidateQueries({ queryKey: [name] });
    }
  }, [cache, status.data?.id, status.data?.status]);
  const receipt = status.data || start.data;
  const saved =
    !receipt || download?.operation_id === receipt.id ? download : null;
  const selecting = !!receipt && ["queued", "running"].includes(receipt.status);
  const busy = start.isPending || selecting || saved?.state === "preparing";
  const error = start.error?.message || status.error?.message;
  const state: SavedDownload["state"] | undefined = start.isPending
    ? "preparing"
    : error
      ? "failed"
      : saved?.state ||
        (receipt
          ? selecting
            ? "preparing"
            : ["held", "failed"].includes(receipt.status)
              ? "failed"
              : receipt.status === "cancelled"
                ? "cancelled"
                : status.data?.download_id
                  ? "queued"
                  : "selected"
          : undefined);
  const failed = state === "failed" || state === "needs-review";
  const tone = failed
    ? "error"
    : state === "imported" || state === "downloaded" || state === "queued"
      ? "success"
      : "info";
  const complete = saved
    ? saved.prevent_download
    : !!receipt && receipt.status === "completed";
  const reasons = [
    ...new Set(
      saved?.reasons ||
        status.data?.decisions
          ?.filter((decision) => decision.result_id === resultId)
          .flatMap((decision) => decision.reasons) ||
        [],
    ),
  ];
  const rawMessage = error || saved?.message || receipt?.message;
  const message = start.isPending
    ? "Checking this release and your download settings."
    : error || (failed && reasons.length ? reasons.join(". ") : rawMessage);
  const requestLink = saved?.request_id
    ? `/requests#request-${saved.request_id}`
    : "/requests";
  return (
    <>
      <button
        className={showLabel ? "primary" : "release-info-button"}
        aria-label={`Download ${title}`}
        title={
          state && (complete || busy)
            ? labels[state]
            : disabledReason || "Download this release"
        }
        disabled={disabled || !!busy || complete}
        onClick={() => {
          if (receipt && !busy) {
            key.current = randomUUID();
            setOperationId(undefined);
          }
          start.mutate(offerWedge && useWedge);
        }}
      >
        {busy ? (
          <LoaderCircle
            size={18}
            className="source-download-spinner"
            aria-hidden
          />
        ) : complete ? (
          <CircleCheck size={18} aria-hidden />
        ) : (
          <Download size={18} aria-hidden />
        )}
        {showLabel &&
          (busy ? "Starting…" : complete && state ? labels[state] : "Download")}
      </button>
      {offerWedge && !complete && (
        <label className="check-label wedge-choice">
          <input
            type="checkbox"
            checked={useWedge}
            disabled={disabled || !!busy}
            onChange={(event) => {
              setUseWedge(event.target.checked);
              if (!busy) key.current = randomUUID();
            }}
          />
          Use a Freeleech wedge
        </label>
      )}
      {state && (
        <div
          className="source-download-message"
          data-tone={tone}
          role={failed ? "alert" : "status"}
        >
          <strong className="source-download-heading">
            {failed ? (
              <CircleAlert size={16} aria-hidden />
            ) : busy || state === "downloading" ? (
              <LoaderCircle
                size={16}
                className="source-download-spinner"
                aria-hidden
              />
            ) : (
              <CircleCheck size={16} aria-hidden />
            )}
            {labels[state]}
          </strong>
          {message && <span>{message}</span>}
          {typeof saved?.progress === "number" && state === "downloading" && (
            <progress
              max={1}
              value={Math.max(0, Math.min(1, saved.progress))}
              aria-label={`${title} download progress`}
            />
          )}
          {failed &&
            /qBittorrent|SABnzbd|NZBGet|download client|downloader|download route/i.test(
              message || "",
            ) && <Link to="/settings#downloaders">Check download clients</Link>}
          {failed && /Settings → Download preferences/i.test(message || "") ? (
            <Link to="/settings#preferences">Choose a default destination</Link>
          ) : failed &&
            /library folder|library destination|import destination|import route|download-to-library/i.test(
              message || "",
            ) ? (
            <Link to="/settings#libraries">Check library folders</Link>
          ) : null}
          {(receipt || saved) && (
            <Link to={requestLink}>
              View request <span aria-hidden>→</span>
            </Link>
          )}
        </div>
      )}
    </>
  );
}
