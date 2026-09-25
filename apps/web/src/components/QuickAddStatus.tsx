import { CircleAlert, CircleCheck, LoaderCircle } from "lucide-react";
import { Link } from "react-router-dom";
import type { components } from "../api/schema";

export default function QuickAddStatus({
  receipt,
  workId,
}: {
  receipt: components["schemas"]["QuickAddView"];
  workId?: string;
}) {
  const active = ["queued", "running"].includes(receipt.status);
  const held = ["held", "failed", "cancelled"].includes(receipt.status);
  const checks = receipt.source_checks || [];
  const failure = [
    receipt.message,
    ...checks.flatMap((check) => check.reasons),
  ].join(" ");
  const routeProblem =
    held &&
    /download route|download client|downloader|qBittorrent|SABnzbd|import destination|library destination/i.test(
      failure,
    );
  const noMatch =
    held &&
    /no (eligible|automatic|matching) release|no results|title and author/i.test(
      failure,
    );
  const message = active
    ? "Finding the best match using your saved preferences."
    : routeProblem
      ? "Check your download client and library folder settings, then try again."
      : noMatch
        ? "No matching download found for one or more requested formats. Your request is saved."
        : held
          ? "Open downloads to see what needs attention."
          : receipt.message;
  const Icon = active ? LoaderCircle : held ? CircleAlert : CircleCheck;
  const slots = [...new Set(checks.map((check) => check.slot))];
  const context = new URLSearchParams({ tab: "sources" });
  if (receipt.request_id) {
    context.set("request", receipt.request_id);
    if (slots.length === 1) context.set("slot", slots[0]);
  }
  return (
    <section
      className="quick-add-status"
      data-tone={held ? "review" : active ? "active" : "success"}
      aria-label="Quick add progress"
    >
      <div
        className="quick-add-status-heading"
        role="status"
        aria-live="polite"
      >
        <Icon
          size={17}
          aria-hidden
          className={active ? "source-download-spinner" : undefined}
        />
        <strong>
          {active
            ? "Finding your download"
            : held
              ? "Request needs attention"
              : "Quick add complete"}
        </strong>
      </div>
      <p>{message}</p>
      <div
        className="quick-add-status-actions"
        role="group"
        aria-label="Quick add next steps"
      >
        {routeProblem && (
          <Link to="/settings#downloaders">Check download settings</Link>
        )}
        {held && workId && (
          <Link to={`/books/${workId}?${context}`}>Review sources</Link>
        )}
        <Link
          to={
            receipt.request_id
              ? `/requests#request-${receipt.request_id}`
              : "/requests"
          }
        >
          View downloads
        </Link>
      </div>
    </section>
  );
}
