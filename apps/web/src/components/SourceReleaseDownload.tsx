import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleAlert, CircleCheck, Download, LoaderCircle } from "lucide-react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api, result, type Auth } from "../api/client";
import type { components } from "../api/schema";
import { randomUUID } from "../randomUUID";

const CollectionReview = lazy(() => import("./CollectionReview"));

type SavedDownload = components["schemas"]["ReleaseDownloadStatus"];
const labels: Record<SavedDownload["state"], string> = {
  preparing: "Starting download",
  queued: "Download queued",
  downloading: "Downloading",
  downloaded: "Downloaded",
  imported: "In library",
  failed: "Download not started",
  cancelled: "Download cancelled",
  selected: "Release prepared",
  "needs-review": "Review release",
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
  row = false,
  possibleCollection = false,
  reviewContext,
}: {
  searchId: string;
  resultId: string;
  title: string;
  disabled: boolean;
  disabledReason?: string;
  download?: SavedDownload | null;
  offerWedge?: boolean;
  showLabel?: boolean;
  row?: boolean;
  possibleCollection?: boolean;
  reviewContext?: string;
}) {
  const [reviewCollection, setReviewCollection] = useState(false);
  const key = useRef(randomUUID());
  const cache = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const { data: session } = useQuery<Auth | null>({
    queryKey: ["session"],
    enabled: false,
  });
  const [operationId, setOperationId] = useState<string>();
  const [useWedge, setUseWedge] = useState(false);
  const inspect = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/source-searches/{search_id}/results/{result_id}/artifact",
          {
            params: {
              path: { search_id: searchId, result_id: resultId },
              query: offerWedge && useWedge ? { use_wedge: true } : {},
            },
          },
        ),
      ),
    onSuccess: (artifact) => {
      cache.setQueryData(["source-artifact", artifact.id], artifact);
      const context = new URLSearchParams(reviewContext);
      if (!context.has("request") && saved?.request_id)
        context.set("request", saved.request_id);
      navigate(`/sources/artifacts/${artifact.id}?${context}`, {
        state: {
          returnTo: location.pathname + location.search,
          bookOrigin: location.state?.returnTo,
        },
      });
    },
  });
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
      for (const name of [
        "requests",
        "downloads",
        "activity",
        "book-sources",
        "quick-add",
      ])
        void cache.invalidateQueries({ queryKey: [name] });
    }
  }, [cache, status.data?.id, status.data?.status]);
  const receipt = status.data || start.data;
  const saved =
    !receipt || download?.operation_id === receipt.id ? download : null;
  const selecting = !!receipt && ["queued", "running"].includes(receipt.status);
  const busy =
    start.isPending ||
    inspect.isPending ||
    selecting ||
    saved?.state === "preparing";
  const error = start.error?.message || status.error?.message;
  const state: SavedDownload["state"] | undefined = start.isPending
    ? "preparing"
    : error
      ? "failed"
      : saved?.state ||
        (receipt
          ? selecting
            ? "preparing"
            : receipt.status === "held"
              ? "needs-review"
              : receipt.status === "failed"
                ? "failed"
                : receipt.status === "cancelled"
                  ? "cancelled"
                  : status.data?.download_id
                    ? "queued"
                    : "selected"
          : undefined);
  const failed = state === "failed" || state === "needs-review";
  const canReview =
    state === "needs-review" &&
    session?.user.role === "admin" &&
    !!reviewContext;
  const tone =
    state === "needs-review"
      ? "info"
      : failed
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
    : null;
  const statusContent = state ? (
    <>
      {failed ? (
        <CircleAlert size={16} aria-hidden />
      ) : busy || ["downloading", "queued"].includes(state) ? (
        <LoaderCircle
          size={16}
          className="source-download-spinner"
          aria-hidden
        />
      ) : (
        <CircleCheck size={16} aria-hidden />
      )}
      <span
        className={
          !showLabel && (busy || ["downloading", "queued"].includes(state))
            ? "sr-only"
            : undefined
        }
      >
        {labels[state]}
      </span>
    </>
  ) : null;
  return (
    <>
      {((showLabel && !row) ||
        !state ||
        (!complete && !busy && !["downloading", "queued"].includes(state))) && (
        <button
          className={showLabel ? "primary" : "release-info-button"}
          aria-label={`${canReview ? "Review release" : "Download"} ${title}`}
          title={
            state && (complete || busy)
              ? labels[state]
              : disabledReason ||
                (canReview
                  ? "Review the manifest and select this release"
                  : "Download this release")
          }
          disabled={disabled || !!busy || complete}
          onClick={() => {
            if (possibleCollection) {
              setReviewCollection(true);
              return;
            }
            if (canReview) {
              inspect.mutate();
              return;
            }
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
          {(showLabel || canReview) &&
            (busy
              ? canReview
                ? "Opening review…"
                : "Starting…"
              : canReview
                ? "Review release"
                : complete && state
                  ? labels[state]
                  : "Download")}
        </button>
      )}
      {reviewCollection && (
        <Suspense fallback={<p role="status">Loading collection review…</p>}>
          <CollectionReview
            searchId={searchId}
            resultId={resultId}
            close={() => setReviewCollection(false)}
          />
        </Suspense>
      )}
      {offerWedge && !possibleCollection && !complete && (
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
      {state &&
        (requestLink ? (
          <Link
            to={requestLink}
            className="source-download-status"
            data-tone={tone}
            title={message || labels[state]}
            aria-label={`${labels[state]}: ${title}. View request`}
          >
            {statusContent}
          </Link>
        ) : (
          <span
            className="source-download-status"
            data-tone={tone}
            title={message || labels[state]}
            role="status"
          >
            {statusContent}
          </span>
        ))}
      {showLabel && failed && message && (
        <p className="error" role="status">
          {message}
        </p>
      )}
      {inspect.error && (
        <p className="error" role="alert">
          {inspect.error.message}
        </p>
      )}
    </>
  );
}
