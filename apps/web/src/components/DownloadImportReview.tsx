import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookOpen,
  Check,
  CircleAlert,
  Download,
  FolderOpen,
  Link2,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import ImportExecution from "./ImportExecution";
import { libraryRelativePath } from "./importPaths";

type Inspection = components["schemas"]["InspectionView"];

export default function DownloadImportReview({
  inspection,
}: {
  inspection: Inspection;
}) {
  const download = inspection.download!;
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
      cache.invalidateQueries({ queryKey: ["requests", "counts"] });
    },
  });
  const done = download.state === "complete";
  const blocked = ["held", "review", "cancelled", "cancel-held"].includes(
    download.state,
  );
  const identified = !!inspection.plan_id;
  const published = done || download.state === "awaiting-library";
  const files = inspection.snapshot?.files || [];
  const media = files.filter(
    (file) => file.medium && file.state === "inspected",
  );
  const extras = files.filter((file) =>
    /\.(jpe?g|png|webp|cue|nfo|txt|sfv|m3u8?|opf)$/i.test(file.path),
  ).length;
  const unresolved = files.length - media.length - extras;
  return (
    <section className="download-import" aria-label="Download import">
      <header className="download-import-book">
        <div className="download-import-cover" aria-hidden="true">
          {download.cover_url ? (
            <img
              src={`/api/catalog/cover-image?url=${encodeURIComponent(download.cover_url)}`}
              alt=""
            />
          ) : (
            <BookOpen />
          )}
        </div>
        <div className="download-import-identity">
          <p className="eyebrow">
            {download.medium === "audio" ? "Audiobook" : "Ebook"}
          </p>
          <h2>
            <Link to={`/books/${download.work_id}`}>{download.title}</Link>
          </h2>
          <p className="muted">{download.authors.join(", ")}</p>
          <span className="download-import-association">
            <Link2 size={14} aria-hidden="true" /> Linked to your requested book
          </span>
        </div>
        <span
          className={`download-import-state ${done ? "is-complete" : blocked ? "is-held" : "is-active"}`}
        >
          {done ? (
            <Check size={16} />
          ) : blocked ? (
            <CircleAlert size={16} />
          ) : (
            <LoaderCircle className="spin" size={16} />
          )}
          {done ? "In library" : blocked ? "Needs attention" : "Importing"}
        </span>
      </header>
      <ol className="download-import-steps" aria-label="Import progress">
        {[
          { title: "Downloaded", complete: true, icon: Download },
          { title: "Book matched", complete: identified, icon: BookOpen },
          { title: "Files organized", complete: published, icon: FolderOpen },
          { title: "In library", complete: done, icon: Check },
        ].map((step, index) => (
          <li key={step.title} className={step.complete ? "is-complete" : ""}>
            <span aria-hidden="true">
              {step.complete ? <Check size={16} /> : <step.icon size={16} />}
            </span>
            <span>{step.title}</span>
            <span className="sr-only">
              {step.complete ? ": complete" : `: step ${index + 1}`}
            </span>
          </li>
        ))}
      </ol>
      <div className="download-import-route">
        <div>
          <span className="muted">Completed download</span>
          <strong>
            {media.length} book {media.length === 1 ? "file" : "files"}
            {extras > 0 ? ` · ${extras} extra files` : ""}
            {unresolved > 0 ? ` · ${unresolved} files need review` : ""}
          </strong>
        </div>
        <div>
          <span className="muted">Library folder</span>
          <strong>{download.destination || "Choose a library folder"}</strong>
        </div>
        <div>
          <span className="muted">File handling</span>
          <strong>
            {download.mode === "hardlink"
              ? "Hardlink · keep seeding"
              : download.mode === "copy"
                ? "Copy · keep originals"
                : "Not configured"}
          </strong>
        </div>
      </div>
      {!inspection.plan_id && (
        <div className={`download-import-next ${blocked ? "is-held" : ""}`}>
          <h3>
            {blocked ? "Continue this import" : "Preparing your library copy"}
          </h3>
          <p role="status">{download.message}</p>
          <p className="muted">
            Your book selection is saved. Matching, naming and library placement
            use that selection and your library settings.
          </p>
          <div className="actions">
            {download.can_retry && (
              <button
                className="primary"
                disabled={retry.isPending}
                onClick={() => retry.mutate()}
              >
                <RefreshCw size={16} />
                {retry.isPending ? "Checking files…" : "Retry automatic import"}
              </button>
            )}
            <Link to="/settings#naming">Library settings</Link>
          </div>
        </div>
      )}
      <Notice error={retry.error || plan.error} />
      {inspection.plan_id && plan.isPending && <Loading />}
      {plan.data && <ImportExecution plan={plan.data} compact />}
      <details className="download-import-files">
        <summary>Files &amp; naming</summary>
        <p className="muted">
          Paths below are relative to your library folder.
        </p>
        {plan.data?.document.plan.items.map((item) => (
          <div className="download-import-file" key={item.group_id}>
            <strong>{libraryRelativePath(item.folder)}</strong>
            {(item.files || []).map((file) => (
              <div key={file.source}>
                <span className="muted">{file.source}</span>
                <span>→ {libraryRelativePath(file.destination)}</span>
              </div>
            ))}
          </div>
        ))}
        {!plan.data &&
          media.map((file) => (
            <div className="download-import-file" key={file.path}>
              {file.path}
            </div>
          ))}
        {extras > 0 && (
          <p className="muted">
            Extra files stay in the download folder. They do not block an
            otherwise valid book import.
          </p>
        )}
      </details>
    </section>
  );
}
