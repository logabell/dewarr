import QuotaSummary from "../components/QuotaSummary";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  BookOpen,
  Check,
  ChevronRight,
  ClipboardCheck,
  CircleAlert,
  CircleCheck,
  LoaderCircle,
  Download,
  Ellipsis,
  ExternalLink,
  Eye,
  Headphones,
  RefreshCw,
  Search,
  Undo2,
  X,
  type LucideIcon,
} from "lucide-react";
import { Link, useNavigate } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import InfiniteScroll from "../components/InfiniteScroll";
import { usePagedQuery } from "../hooks/usePagedQuery";
import { randomUUID } from "../randomUUID";
import DownloadConstraints from "./DownloadConstraints";
import DownloadRepair from "./DownloadRepair";
import DownloadRecoveryDetails from "../components/DownloadRecoveryDetails";
import { EffectivePreferences } from "./PreferenceFields";
import {
  nextRequestOffset,
  requestCountLabel,
  type RequestFilter,
} from "./requestFilters";
import { liveDownloadStates, statusLabel } from "./requestStatus";
import { EffectiveScope } from "./ScopeFields";

type Request = components["schemas"]["RequestView"];
type Target = components["schemas"]["TargetView"];

export type { RequestFilter };

const emptyCopy: Record<RequestFilter, string> = {
  all: "No active requests.",
  pending: "No requests are waiting for approval.",
  downloading: "No downloads yet.",
  library: "No requests are in the library yet.",
  declined: "No declined requests.",
  withdrawn: "No withdrawn requests.",
  review: "No downloads need review.",
};

function mediumLabel(slot: string) {
  if (slot === "audio") return "Audiobook";
  if (slot === "either") return "Either";
  return "Ebook";
}

function chipState(label: string) {
  if (label === "Needs review") return "paused";
  if (label === "Download not started") return "failed";
  if (label === "Preparing download") return "downloading";
  if (label === "In library") return "satisfied";
  if (label === "Downloading" || label === "Importing") return "downloading";
  if (label === "Pending" || label === "Paused" || label === "Check inventory")
    return "paused";
  if (label === "Declined" || label === "Withdrawn") return "cancelled";
  return "wanted";
}

function targetNotes(label: string, target: Target) {
  const redundant = new Set([
    label,
    "Request declined",
    "Waiting for approval",
  ]);
  const parts = [
    target.message,
    target.attempt_message,
    target.repair_message,
    target.review_message,
    ...(target.transfer_notes ?? []),
  ].filter((part): part is string => !!part && !redundant.has(part));
  return [...new Set(parts)];
}

type RowAction = {
  key: string;
  label: string;
  icon: LucideIcon;
  href?: string;
  danger?: boolean;
  onSelect?: () => void;
};

function ActionMenu({
  label,
  items,
  disabled,
}: {
  label: string;
  items: RowAction[];
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    root.current?.querySelector<HTMLElement>("[role='menuitem']")?.focus();
    function onPointer(event: PointerEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
        return;
      }
      if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
      if (!root.current?.contains(event.target as Node)) return;
      const entries = [
        ...(root.current?.querySelectorAll<HTMLElement>("[role='menuitem']") ??
          []),
      ];
      if (!entries.length) return;
      const index = entries.indexOf(document.activeElement as HTMLElement);
      const step = event.key === "ArrowDown" ? 1 : -1;
      entries[(index + step + entries.length) % entries.length]?.focus();
      event.preventDefault();
    }
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  if (!items.length) return null;
  return (
    <div className="request-menu" ref={root}>
      <button
        ref={trigger}
        type="button"
        className="control-icon"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
      >
        <Ellipsis size={16} aria-hidden />
      </button>
      {open && (
        <div className="request-menu-panel" role="menu" aria-label={label}>
          {items.map((item) => {
            const Icon = item.icon;
            return item.href ? (
              <Link
                key={item.key}
                role="menuitem"
                to={item.href}
                onClick={() => setOpen(false)}
              >
                <Icon size={14} aria-hidden />
                {item.label}
              </Link>
            ) : (
              <button
                key={item.key}
                type="button"
                role="menuitem"
                data-danger={item.danger ? "true" : undefined}
                disabled={disabled}
                onClick={() => {
                  setOpen(false);
                  item.onSelect?.();
                }}
              >
                <Icon size={14} aria-hidden />
                {item.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function requester(request: Request) {
  if (request.reasons.some((reason) => reason.label === "Your request"))
    return "You";
  return request.owner_name || "Requested";
}

function shortDate(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

function inProgress(request: Request) {
  return request.targets.some(
    (target) =>
      (target.attempt_state && liveDownloadStates.has(target.attempt_state)) ||
      ["queued", "inspecting", "importing"].includes(target.import_state || ""),
  );
}

function expectedStatus(request: Request) {
  if (
    request.approval_status === "approved" ||
    request.approval_status === "declined"
  )
    return request.approval_status;
  return "pending" as const;
}

export default function ActivityRequests({
  canManage,
  status,
  sort,
}: {
  canManage: boolean;
  status: RequestFilter;
  sort: "newest" | "title";
}) {
  const cache = useQueryClient();
  const navigate = useNavigate();
  const claimKeys = useRef(new Map<string, string>());
  const requests = usePagedQuery({
    queryKey: ["requests", "board", status, sort],
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/requests", {
          signal,
          params: {
            query: {
              offset,
              limit: 10,
              sort,
              ...(status === "all" ? { active_only: true } : { status }),
            },
          },
        }),
      ),
    refetchInterval: (query) =>
      query.state.data?.pages.some((page) => page.items.some(inProgress))
        ? 3000
        : 15000,
    staleTime: 0,
    gcTime: 0,
    retry: false,
    initial: 0,
    next: (page, pages, requested) => nextRequestOffset(page, pages, requested),
  });
  const refresh = async () => {
    await Promise.all(
      [
        "requests",
        "request-preview",
        "activity",
        "downloads",
        "list-monitor",
        "book-sources",
        "source-request",
        "quick-add",
      ].map((key) => cache.invalidateQueries({ queryKey: [key] })),
    );
  };
  const withdraw = useMutation({
    mutationFn: async ({
      intent,
      reason,
    }: {
      intent: string;
      reason: string;
    }) =>
      result(
        await api.DELETE("/api/requests/{intent_id}/reasons/{reason_id}", {
          params: { path: { intent_id: intent, reason_id: reason } },
        }),
      ),
    onSuccess: refresh,
  });
  const decide = useMutation({
    mutationFn: async ({
      id,
      status: decision,
      download,
      expected,
    }: {
      id: string;
      status: "approved" | "declined";
      download: boolean;
      expected: "pending" | "approved" | "declined";
    }) =>
      result(
        await api.POST("/api/requests/{intent_id}/decision", {
          params: {
            path: { intent_id: id },
            header: { "idempotency-key": randomUUID() },
          },
          body: { status: decision, download, expected_status: expected },
        }),
      ),
    onSuccess: async () => {
      await cache.invalidateQueries({ queryKey: ["requests"] });
    },
  });
  const transfer = useMutation({
    mutationFn: async ({ id, cancel }: { id: string; cancel: boolean }) => {
      const params = { path: { attempt_id: id } };
      return result(
        cancel
          ? await api.DELETE("/api/acquisition/downloads/{attempt_id}", {
              params,
            })
          : await api.POST("/api/acquisition/downloads/{attempt_id}/recheck", {
              params,
            }),
      );
    },
    onSuccess: refresh,
  });
  const claim = useMutation({
    mutationFn: async (target: Target) => {
      const revision = `${target.attempt_id}:${target.review_revision}`;
      if (!claimKeys.current.has(revision))
        claimKeys.current.set(revision, randomUUID());
      return result(
        await api.POST("/api/acquisition/reviews/{attempt_id}/claim", {
          params: {
            path: { attempt_id: target.attempt_id! },
            header: { "idempotency-key": claimKeys.current.get(revision)! },
          },
          body: { revision: target.review_revision! },
        }),
      );
    },
    onSuccess: (review) => {
      if (review.inspection_id)
        navigate(
          `/organization/inspections?inspection=${review.inspection_id}`,
        );
    },
    onSettled: refresh,
  });
  return (
    <section className="requests-board" aria-label="Requests">
      <QuotaSummary />
      <Notice
        error={
          requests.error ||
          withdraw.error ||
          decide.error ||
          transfer.error ||
          claim.error
        }
      />
      {requests.isPending && <Loading />}
      {requests.error && (
        <button
          type="button"
          disabled={requests.isFetching}
          onClick={() => requests.refetch()}
        >
          Retry requests
        </button>
      )}
      {requests.data && (
        <>
          <p className="requests-count muted" role="status">
            {requestCountLabel(
              requests.data.items.length,
              requests.data.total,
              !!requests.data.total_bounded,
              requests.hasNextPage,
            )}
          </p>
          {!requests.data.items.length && !requests.hasNextPage && (
            <p className="requests-empty">{emptyCopy[status]}</p>
          )}
          <div className="request-ledger-scroll">
            <table className="request-ledger">
              <caption className="sr-only">
                Book requests and download activity
              </caption>
              <thead>
                <tr>
                  <th>Book</th>
                  <th>Format</th>
                  <th>Status</th>
                  <th>Progress</th>
                  <th>Speed</th>
                  <th>Time left</th>
                  <th>
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              {requests.data.items.map((request) => (
                <RequestCard
                  key={request.id}
                  request={request}
                  canManage={canManage}
                  busy={
                    withdraw.isPending ||
                    decide.isPending ||
                    transfer.isPending ||
                    claim.isPending
                  }
                  onWithdraw={(reason) =>
                    withdraw.mutate({ intent: request.id, reason })
                  }
                  onDecide={(decision, download) =>
                    decide.mutate({
                      id: request.id,
                      status: decision,
                      download,
                      expected: expectedStatus(request),
                    })
                  }
                  onTransfer={(id, cancel) => transfer.mutate({ id, cancel })}
                  onClaim={(target) => claim.mutate(target)}
                />
              ))}
            </table>
          </div>
          {decide.data?.download_message && (
            <p className="notice" role="status">
              {decide.data.download_message}
            </p>
          )}
          <InfiniteScroll query={requests} />
        </>
      )}
    </section>
  );
}

function RequestCover({ title, url }: { title: string; url?: string | null }) {
  const [failed, setFailed] = useState(false);
  return (
    <div className="request-cover" aria-hidden="true">
      {url && !failed ? (
        <img src={url} alt="" onError={() => setFailed(true)} />
      ) : (
        <span>{title}</span>
      )}
    </div>
  );
}

function targetActions(
  request: Request,
  target: Target,
  canManage: boolean,
  onTransfer: (attemptId: string, cancel: boolean) => void,
  onClaim: (target: Target) => void,
) {
  const qualify = (label: string) =>
    request.targets.length > 1
      ? `${label} · ${mediumLabel(target.slot)}`
      : label;
  const actions: RowAction[] = [];
  if (target.next_action === "search" && request.can_open_book)
    actions.push({
      key: `search-${target.slot}`,
      label: "Choose release",
      icon: Search,
      href: `/books/${request.work_id}?tab=sources&request=${request.id}&slot=${target.slot}`,
    });
  if (
    target.needs_review &&
    target.can_claim &&
    target.attempt_id &&
    target.review_revision
  ) {
    const label = target.review_retry
      ? "Retry"
      : target.review_reassignment
        ? "Reassign"
        : "Review files";
    actions.push({
      key: `claim-${target.slot}`,
      label: qualify(label),
      icon: ClipboardCheck,
      onSelect: () => onClaim(target),
    });
  }
  if (canManage && target.can_cancel && target.attempt_id)
    actions.push({
      key: `cancel-${target.slot}`,
      label: target.shared_download
        ? "Cancel for every book"
        : qualify("Cancel download"),
      icon: X,
      danger: true,
      onSelect: () => onTransfer(target.attempt_id!, true),
    });
  if (canManage && target.can_recheck && target.attempt_id)
    actions.push({
      key: `recheck-${target.slot}`,
      label: qualify("Recheck"),
      icon: RefreshCw,
      onSelect: () => onTransfer(target.attempt_id!, false),
    });
  if (target.next_action === "selected-release" && target.source_artifact_id)
    actions.push({
      key: `release-${target.slot}`,
      label: qualify("Release"),
      icon: ExternalLink,
      href: `/sources/artifacts/${target.source_artifact_id}`,
    });
  if (target.inspection_id)
    actions.unshift({
      key: `inspect-${target.slot}`,
      label: qualify(target.needs_review ? "Review files" : "Inspect"),
      icon: Eye,
      href: `/organization/inspections?inspection=${target.inspection_id}`,
    });
  return actions;
}

function ActionButton({ action, busy }: { action: RowAction; busy: boolean }) {
  const Icon = action.icon;
  if (action.href)
    return (
      <Link className="control-action" to={action.href}>
        <Icon size={14} aria-hidden />
        {action.label}
      </Link>
    );
  return (
    <button type="button" disabled={busy} onClick={action.onSelect}>
      <Icon size={14} aria-hidden />
      {action.label}
    </button>
  );
}

function RequestCard({
  request,
  canManage,
  busy,
  onWithdraw,
  onDecide,
  onTransfer,
  onClaim,
}: {
  request: Request;
  canManage: boolean;
  busy: boolean;
  onWithdraw: (reasonId: string) => void;
  onDecide: (status: "approved" | "declined", download: boolean) => void;
  onTransfer: (attemptId: string, cancel: boolean) => void;
  onClaim: (target: Target) => void;
}) {
  const when = shortDate(request.created_at);
  const byline = [
    request.authors?.length ? request.authors.join(", ") : request.description,
    requester(request),
    when,
  ]
    .filter(Boolean)
    .join(" · ");
  const menu: RowAction[] = [];
  if (request.can_decide)
    menu.push({
      key: "decline",
      label: "Decline",
      icon: X,
      danger: true,
      onSelect: () => onDecide("declined", false),
    });
  if (request.can_withdraw)
    for (const reason of request.reasons.filter((item) => item.active))
      menu.push({
        key: `withdraw-${reason.id}`,
        label: `Withdraw ${reason.label.toLowerCase()}`,
        icon: Undo2,
        onSelect: () => onWithdraw(reason.id),
      });
  const rows = request.targets.map((target) => {
    const actions = targetActions(
      request,
      target,
      canManage,
      onTransfer,
      onClaim,
    );
    const primary = actions[0];
    if (actions.length > 1) menu.push(...actions.slice(1));
    return { target, primary };
  });
  const [expanded, setExpanded] = useState(false);
  return (
    <tbody
      id={`request-${request.id}`}
      aria-label={`${request.work_title} request`}
    >
      {rows.map(({ target, primary }, index) => {
        const label = statusLabel(request, target);
        const active = label === "Downloading";
        const spinning = [
          "Downloading",
          "Importing",
          "Preparing download",
        ].includes(label);
        const progress =
          typeof target.progress === "number"
            ? Math.max(0, Math.min(1, target.progress))
            : null;
        const notes = targetNotes(label, target);
        return (
          <tr key={target.slot} className="request-ledger-row">
            <td>
              <div className="request-book-cell">
                <RequestCover
                  title={request.work_title}
                  url={request.cover_url}
                />
                <div className="request-row-identity">
                  <h2>
                    {request.can_open_book ? (
                      <Link to={`/books/${request.work_id}`}>
                        {request.work_title}
                      </Link>
                    ) : (
                      request.work_title
                    )}
                  </h2>
                  <p className="request-row-meta">{byline}</p>
                </div>
              </div>
            </td>
            <td>
              <span className="request-medium">
                {target.slot === "audio" ? (
                  <Headphones size={14} aria-hidden />
                ) : (
                  <BookOpen size={14} aria-hidden />
                )}
                {mediumLabel(target.slot)}
              </span>
            </td>
            <td>
              <span
                className="request-status"
                data-state={chipState(label)}
                title={notes.join(". ")}
              >
                {spinning ? (
                  <LoaderCircle
                    size={14}
                    className="source-download-spinner"
                    aria-hidden
                  />
                ) : label === "In library" ? (
                  <CircleCheck size={14} aria-hidden />
                ) : label === "Needs review" ||
                  label === "Download not started" ? (
                  <CircleAlert size={14} aria-hidden />
                ) : null}
                {label}
              </span>
              {label === "Needs review" && (
                <p className="request-status-hint">
                  {target.inspection_id || target.can_claim
                    ? "Choose Review files to continue"
                    : "Administrator review required"}
                </p>
              )}
            </td>
            <td>
              <div className="request-transfer-progress">
                {progress !== null ? (
                  <>
                    <span className="request-percent">
                      {Math.round(progress * 100)}%
                    </span>
                    <progress
                      max={1}
                      value={progress}
                      aria-label={`${request.work_title} ${mediumLabel(target.slot).toLowerCase()} download progress`}
                    />
                  </>
                ) : active ? (
                  <span className="muted">Connecting…</span>
                ) : (
                  <span className="muted">—</span>
                )}
              </div>
            </td>
            <td className="request-metric">
              {active ? transferSpeed(target.download_speed) : "—"}
            </td>
            <td className="request-metric">
              {active ? transferTime(target.eta_seconds) : "—"}
            </td>
            <td>
              <div className="request-row-tools">
                {index === 0 && request.can_decide && (
                  <button
                    className="primary"
                    disabled={busy}
                    onClick={() => onDecide("approved", false)}
                  >
                    <Check size={14} aria-hidden />
                    Approve
                  </button>
                )}
                {index === 0 && request.can_start_download && (
                  <button
                    disabled={busy}
                    onClick={() => onDecide("approved", true)}
                  >
                    <Download size={14} aria-hidden />
                    Download
                  </button>
                )}
                {primary && <ActionButton action={primary} busy={busy} />}
                {index === 0 && (
                  <>
                    <button
                      className="control-icon"
                      aria-label={`Details for ${request.work_title}`}
                      aria-expanded={expanded}
                      onClick={() => setExpanded(!expanded)}
                    >
                      <ChevronRight
                        size={16}
                        className={expanded ? "request-chevron-open" : ""}
                        aria-hidden
                      />
                    </button>
                    <ActionMenu
                      label={`Actions for ${request.work_title}`}
                      items={menu}
                      disabled={busy}
                    />
                  </>
                )}
              </div>
            </td>
          </tr>
        );
      })}
      {expanded && (
        <tr className="request-detail-row">
          <td colSpan={7}>
            <div className="request-detail-content">
              {request.targets.map((target) => (
                <div key={target.slot}>
                  {targetNotes(statusLabel(request, target), target).map(
                    (note) => (
                      <p className="request-target-note" key={note}>
                        {note}
                      </p>
                    ),
                  )}
                  {!!target.shared_books?.length && (
                    <p className="request-target-note">
                      Shared with {target.shared_books.join(", ")}
                    </p>
                  )}
                  {target.can_repair && target.attempt_id && (
                    <DownloadRepair attemptId={target.attempt_id} />
                  )}
                  {target.attempt_id && target.can_view_download_history && (
                    <DownloadRecoveryDetails
                      attemptId={target.attempt_id}
                      workId={request.work_id}
                    />
                  )}
                </div>
              ))}
              <ul className="request-reasons">
                {request.reasons.map((reason) => (
                  <li key={reason.id}>
                    {reason.label}
                    {reason.active ? "" : " · Withdrawn"}
                    {reason.decision_note && <p>{reason.decision_note}</p>}
                  </li>
                ))}
              </ul>
              <RequestDetails request={request} />
            </div>
          </td>
        </tr>
      )}
    </tbody>
  );
}

function transferSpeed(value?: number | null) {
  if (value == null) return "—";
  if (value >= 1048576) return `${(value / 1048576).toFixed(1)} MiB/s`;
  if (value >= 1024) return `${Math.round(value / 1024)} KiB/s`;
  return `${value} B/s`;
}

function transferTime(value?: number | null) {
  if (value == null || value >= 8640000) return "—";
  if (value < 60) return "< 1m";
  if (value < 3600) return `${Math.ceil(value / 60)}m`;
  return `${Math.floor(value / 3600)}h ${Math.ceil((value % 3600) / 60)}m`;
}

function RequestDetails({ request }: { request: Request }) {
  const [open, setOpen] = useState(false);
  return (
    <details
      className="request-preferences"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <ChevronRight
          size={14}
          aria-hidden
          className="request-details-chevron"
        />
        Details
      </summary>
      {open && (
        <div className="request-preferences-body">
          <EffectiveScope
            specification={request.specification}
            origins={request.release_policy?.scope_origins}
          />
          {request.release_policy && (
            <EffectivePreferences
              preferences={request.release_policy.preferences}
              origins={request.release_policy.origins || {}}
            />
          )}
          <DownloadConstraints
            value={request.specification.download_constraints}
          />
        </div>
      )}
    </details>
  );
}
