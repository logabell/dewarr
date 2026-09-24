export const liveDownloadStates = new Set([
  "queued",
  "preflight",
  "submitting",
  "uncertain",
  "downloading",
  "held",
]);

type StatusRequest = {
  approval_status: string;
  reasons: { active: boolean }[];
};

type StatusTarget = {
  state: string;
  message?: string | null;
  attempt_state?: string | null;
  selection_status?: string | null;
  next_action?: string | null;
};

export function statusLabel(request: StatusRequest, target: StatusTarget) {
  if (!request.reasons.some((reason) => reason.active)) return "Withdrawn";
  if (
    request.approval_status === "declined" ||
    target.message === "Request declined"
  )
    return "Declined";
  if (
    request.approval_status === "pending" ||
    target.message === "Waiting for approval"
  )
    return "Pending";
  if (target.attempt_state && liveDownloadStates.has(target.attempt_state))
    return "Downloading";
  if (target.state === "awaiting-inventory") return "Check inventory";
  if (target.state === "paused") return "Paused";
  if (target.attempt_state === "complete" && target.state !== "satisfied")
    return "Importing";
  if (target.state === "satisfied") return "In library";
  if (
    target.next_action === "downloads" &&
    target.attempt_state !== "cancelled"
  )
    return "Downloading";
  if (["held", "failed"].includes(target.selection_status || ""))
    return "Download not started";
  if (["queued", "running"].includes(target.selection_status || ""))
    return "Preparing download";
  if (target.state === "wanted") return "Wanted";
  if (target.state === "cancelled") return "Withdrawn";
  return target.state;
}
