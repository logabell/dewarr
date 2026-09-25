import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { BookOpen, ChevronDown, Download, Headphones } from "lucide-react";
import { Link } from "react-router-dom";
import { api, result, type Auth } from "../api/client";
import { canStartDownload } from "../permissions";
import { Notice } from "../components";
import { randomUUID } from "../randomUUID";
import QuickAddStatus from "./QuickAddStatus";

type Mode = "both" | "ebook" | "audio" | undefined;
const MISSING_MEDIA = "Choose media to request or set a default";

function PreferencePrompt({ cover = false }: { cover?: boolean }) {
  const hint = useId();
  return (
    <div className={cover ? "cover-quick-preference" : "quick-add-preference"}>
      <p id={hint}>Preference not set</p>
      <Link
        className={cover ? "cover-quick-set" : "button-link"}
        to="/settings#preferences"
        aria-describedby={hint}
      >
        Set here
      </Link>
    </div>
  );
}
export default function QuickAdd({
  workId,
  resolveWork,
  coverFormats,
  actionLabel = "Quick add",
}: {
  coverFormats?: { ebook: boolean; audio: boolean };
  workId?: string;
  resolveWork?: () => Promise<string>;
  actionLabel?: string;
}) {
  const cache = useQueryClient();
  const { data: session } = useQuery<Auth | null>({
    queryKey: ["session"],
    enabled: false,
  });
  const grants = session?.user.permissions;
  const role = session?.user.role;
  const ebookDownload = canStartDownload(grants, role, "ebook");
  const audioDownload = canStartDownload(grants, role, "audio");
  const eitherDownload = canStartDownload(grants, role);
  const [engaged, setEngaged] = useState(!coverFormats);
  const menu = useRef<HTMLDetailsElement>(null);
  const stopWatching = useRef<(() => void) | null>(null);
  const bind = useCallback((node: HTMLDivElement | null) => {
    stopWatching.current?.();
    stopWatching.current = null;
    const parent = node?.closest(".book-link") || node?.closest(".book-cover");
    if (!parent) return;
    const enter = () => setEngaged(true);
    parent.addEventListener("mouseenter", enter);
    parent.addEventListener("focusin", enter);
    stopWatching.current = () => {
      parent.removeEventListener("mouseenter", enter);
      parent.removeEventListener("focusin", enter);
    };
  }, []);
  useEffect(() => () => stopWatching.current?.(), []);
  const [resolvedId, setResolvedId] = useState(workId);
  const id = workId || resolvedId;
  const command = useRef({ mode: undefined as Mode, key: randomUUID() });
  const defaults = useQuery({
    queryKey: ["quick-add-defaults"],
    queryFn: async () =>
      result(
        await api.GET("/api/acquisition/preferences/{scope}", {
          params: { path: { scope: "personal" } },
        }),
      ),
    enabled: engaged,
    staleTime: coverFormats ? 60_000 : 0,
  });
  const status = useQuery({
    queryKey: ["quick-add", id],
    enabled: !!id && engaged,
    queryFn: async () =>
      result(
        await api.GET("/api/requests/quick-add/latest/{work_id}", {
          params: { path: { work_id: id! } },
        }),
      ),
    refetchInterval: (query) =>
      query.state.data &&
      ["queued", "running"].includes(query.state.data.status)
        ? 1500
        : false,
  });
  const add = useMutation({
    mutationFn: async (mode: Mode) => {
      if (command.current.mode !== mode)
        command.current = { mode, key: randomUUID() };
      const work = id || (await resolveWork!());
      setResolvedId(work);
      const receipt = result(
        await api.POST("/api/requests/quick-add", {
          params: { header: { "idempotency-key": command.current.key } },
          body: { work_id: work, specification: mode ? { mode } : {} },
        }),
      );
      await cache.cancelQueries({ queryKey: ["quick-add", work] });
      cache.setQueryData(["quick-add", work], receipt);
      return receipt;
    },
    onSuccess: () => {
      command.current.key = randomUUID();
      for (const name of ["requests", "activity", "downloads"])
        void cache.invalidateQueries({ queryKey: [name] });
    },
  });
  const busy =
    add.isPending ||
    !!(status.data && ["queued", "running"].includes(status.data.status));
  useEffect(() => {
    if (status.data && !["queued", "running"].includes(status.data.status)) {
      for (const name of ["requests", "activity", "downloads", "book-sources"])
        void cache.invalidateQueries({ queryKey: [name] });
    }
  }, [cache, status.data?.id, status.data?.status]);
  const preference = defaults.data?.effective.desired_media;
  const preferenceUnset = defaults.isSuccess && preference == null;
  const [needsPreference, setNeedsPreference] = useState(false);
  const preferenceError = add.error?.message === MISSING_MEDIA;
  const missingPreference =
    (needsPreference || preferenceError) &&
    (preferenceUnset || !defaults.isSuccess);
  const feedbackError =
    (preferenceError ? null : add.error) || status.error || null;
  const label =
    preference === "audio"
      ? "Audiobook"
      : preference === "ebook"
        ? "Ebook"
        : preference === "both"
          ? "Ebook + audiobook"
          : preference === "either"
            ? "Either format"
            : "Saved preferences";
  function choose(mode: Mode) {
    if (menu.current) menu.current.open = false;
    setEngaged(true);
    if (
      mode == null &&
      (preferenceUnset || (!defaults.isSuccess && preferenceError))
    ) {
      setNeedsPreference(true);
      return;
    }
    setNeedsPreference(false);
    add.mutate(mode);
  }
  if (!ebookDownload && !audioDownload) return null;
  if (coverFormats)
    return (
      <div
        ref={bind}
        className={`cover-quick-add ${missingPreference || feedbackError || (engaged && status.data) ? "has-feedback" : ""}`}
        onMouseEnter={() => setEngaged(true)}
        onFocus={() => setEngaged(true)}
      >
        <button
          type="button"
          aria-label="Quick add from cover"
          className="primary"
          disabled={busy || !eitherDownload}
          onClick={() => choose(undefined)}
          title={
            !eitherDownload
              ? "Choose a format you can download"
              : preferenceUnset
                ? "Quick add preference not set"
                : `${actionLabel} · ${label}. Uses your saved format priorities.`
          }
        >
          <Download size={16} aria-hidden="true" />
          {busy ? "Adding…" : actionLabel}
        </button>
        <div className="cover-quick-formats">
          <button
            type="button"
            disabled={busy || coverFormats.ebook || !ebookDownload}
            aria-label={
              coverFormats.ebook ? "Ebook already in library" : "Download ebook"
            }
            title={
              coverFormats.ebook ? "Ebook already in library" : "Download ebook"
            }
            onClick={() => choose("ebook")}
          >
            <BookOpen size={18} aria-hidden="true" />
          </button>
          <button
            type="button"
            disabled={busy || coverFormats.audio || !audioDownload}
            aria-label={
              coverFormats.audio
                ? "Audiobook already in library"
                : "Download audiobook"
            }
            title={
              coverFormats.audio
                ? "Audiobook already in library"
                : "Download audiobook"
            }
            onClick={() => choose("audio")}
          >
            <Headphones size={18} aria-hidden="true" />
          </button>
        </div>
        {missingPreference ? (
          <PreferencePrompt cover />
        ) : (
          <Notice error={feedbackError} />
        )}
        {engaged && status.data && !missingPreference && (
          <span className="cover-quick-status" role="status">
            {["held", "failed"].includes(status.data.status)
              ? "Request needs attention."
              : status.data.message}{" "}
            <Link
              to={
                id && ["held", "failed"].includes(status.data.status)
                  ? `/books/${id}?tab=sources`
                  : "/requests"
              }
            >
              {id && ["held", "failed"].includes(status.data.status)
                ? "Review sources"
                : "View downloads"}
            </Link>
          </span>
        )}
      </div>
    );
  return (
    <div className="quick-add" ref={bind}>
      <div className="quick-add-split">
        <button
          className="primary"
          disabled={busy || !eitherDownload}
          onClick={() => choose(undefined)}
          title={
            !eitherDownload
              ? "Choose a format you can download"
              : preferenceUnset
                ? "Quick add preference not set"
                : actionLabel === "Quick add"
                  ? `Quick add · ${label}. Uses your saved format priorities.`
                  : `${actionLabel} · ${label}. Looks for a file today using your saved format priorities.`
          }
        >
          <Download size={16} />
          {busy ? "Adding…" : actionLabel}
        </button>
        <details
          ref={menu}
          className="quick-add-menu"
          onBlur={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget))
              event.currentTarget.open = false;
          }}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.currentTarget.open = false;
              event.currentTarget.querySelector("summary")?.focus();
            }
          }}
        >
          <summary aria-label={`${actionLabel} format`} title="Choose a format">
            <ChevronDown size={16} />
          </summary>
          <div className="quick-add-options">
            <button
              disabled={busy || !eitherDownload}
              onClick={() => choose("both")}
            >
              Both
            </button>
            <button
              disabled={busy || !ebookDownload}
              onClick={() => choose("ebook")}
            >
              Ebook
            </button>
            <button
              disabled={busy || !audioDownload}
              onClick={() => choose("audio")}
            >
              Audiobook
            </button>
            <small>Uses your saved format priorities.</small>
          </div>
        </details>
      </div>
      {missingPreference ? (
        <PreferencePrompt />
      ) : (
        <>
          <Notice error={feedbackError} />
          {add.error && (
            <Link to="/settings#preferences">Download preferences</Link>
          )}
        </>
      )}
      {status.data && !missingPreference && (
        <QuickAddStatus receipt={status.data} workId={id} />
      )}
    </div>
  );
}
