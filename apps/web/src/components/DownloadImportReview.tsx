import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Check, CircleAlert, LoaderCircle } from "lucide-react";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import ImportExecution from "./ImportExecution";
import DownloadMatchReview from "./DownloadMatchReview";
import BookCover from "./BookCover";
import BookDialog from "./BookDialog";

type Inspection = components["schemas"]["InspectionView"];

export default function DownloadImportReview({
  inspection,
}: {
  inspection: Inspection;
}) {
  const download = inspection.download!;
  const colon = download.title.indexOf(": ");
  const split = download.title.length > 80 && colon > 5 && colon < 90;
  const title = split ? download.title.slice(0, colon) : download.title;
  const subtitle = split ? download.title.slice(colon + 2) : null;
  const [showFiles, setShowFiles] = useState(false);
  const cache = useQueryClient();
  const plan = useQuery({
    queryKey: ["frozen-import-plan", inspection.plan_id],
    enabled: !!inspection.plan_id,
    queryFn: async () =>
      result(
        await api.GET("/api/organization/plans/{plan_id}", {
          params: { path: { plan_id: inspection.plan_id! } },
        }),
      ),
  });
  const retry = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/organization/inspections/{inspection_id}/retry", {
          params: { path: { inspection_id: inspection.id } },
        }),
      ),
    onSuccess: (value) => {
      cache.setQueryData(["inspection", inspection.id], value);
      cache.invalidateQueries({ queryKey: ["requests"] });
      cache.invalidateQueries({
        queryKey: ["download-review-context", inspection.id],
      });
    },
  });
  const done = download.state === "complete";
  const blocked = ["held", "review", "cancelled", "cancel-held"].includes(
    download.state,
  );
  const files = inspection.snapshot?.files || [];
  const media = files.filter((file) => file.medium);
  const needsDecision =
    (!inspection.plan_id || download.state === "cancelled") &&
    inspection.state === "ready" &&
    blocked;
  return (
    <section className="download-review" aria-label="Download import">
      <header className="download-review-book">
        <div className="download-review-cover">
          <BookCover
            title={title}
            cover={download.cover_url}
            medium={download.medium === "audio" ? "audio" : "ebook"}
          />
        </div>
        <div className="download-review-identity">
          <p className="eyebrow">
            {download.medium === "audio" ? "Audiobook" : "Ebook"}
          </p>
          <h2>
            <Link to={`/books/${download.work_id}`}>{title}</Link>
          </h2>
          {subtitle && <p className="download-review-subtitle">{subtitle}</p>}
          <p className="download-review-author">
            {download.authors.join(", ")}
          </p>
          <Link
            className="download-review-book-link"
            to={`/books/${download.work_id}`}
          >
            View book details →
          </Link>
        </div>
      </header>
      <div className="download-review-status">
        <span
          className={`download-import-state ${done ? "is-complete" : blocked ? "is-held" : "is-active"}`}
        >
          {done ? (
            <Check size={16} />
          ) : blocked ? (
            <CircleAlert size={16} />
          ) : (
            <LoaderCircle size={16} className="spin" />
          )}
          {done
            ? "In library"
            : blocked
              ? "Needs attention"
              : "Adding to library"}
        </span>
        <span className="muted">
          Download complete
          {media.length
            ? ` · ${media.length} ${media.length === 1 ? "file" : "files"}`
            : ""}
        </span>
        <button
          className="download-view-files"
          onClick={() => setShowFiles(true)}
        >
          View files
        </button>
      </div>
      {needsDecision ? (
        <DownloadMatchReview
          inspection={inspection}
          retry={() => retry.mutate()}
          retrying={retry.isPending}
        />
      ) : plan.data ? (
        <ImportExecution plan={plan.data} compact />
      ) : !plan.isPending || !inspection.plan_id ? (
        <div className="download-attention" role="status">
          <h3>
            {inspection.state === "failed"
              ? "We couldn’t read this download"
              : "Adding your book to the library"}
          </h3>
          <p>
            {inspection.state === "failed"
              ? "Check that the download finished and its files are accessible, then try again."
              : "Your book is already selected. File checks and library preparation happen automatically."}
          </p>
          {download.can_retry && (
            <button
              className="primary"
              onClick={() => retry.mutate()}
              disabled={retry.isPending}
            >
              Check download again
            </button>
          )}
        </div>
      ) : (
        <Loading />
      )}
      <Notice error={retry.error || plan.error} />
      {showFiles && (
        <BookDialog title="Downloaded files" close={() => setShowFiles(false)}>
          <p className="muted">
            Files stay in the download folder while a library copy is added.
          </p>
          <ul className="download-file-list">
            {files.map((file) => (
              <li key={file.path}>
                <span>{file.path.split("/").pop()}</span>
                <small>
                  {file.medium ||
                  /\.(epub|pdf|cbz|m4b|m4a|mp3|flac|ogg|opus|aac|wav|wma)$/i.test(
                    file.path,
                  )
                    ? file.state === "inspected"
                      ? "Ready"
                      : "Needs attention"
                    : "Extra file"}
                </small>
                {file.state === "held" && file.reason && (
                  <p className="download-file-reason">{file.reason}</p>
                )}
              </li>
            ))}
          </ul>
        </BookDialog>
      )}
    </section>
  );
}
