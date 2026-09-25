import QuotaSummary from "../components/QuotaSummary";
import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "../components/InfiniteScroll";
import { EffectiveScope } from "./ScopeFields";
import RequestPreferences, { type Choice } from "./RequestPreferences";
import { EffectivePreferences } from "./PreferenceFields";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result, type Auth } from "../api/client";
import {
  canAutoDownload,
  canRequestAdvanced,
  canRequestMedium,
} from "../permissions";
import type { components } from "../api/schema";
import { Notice } from "../components";
import { Link } from "react-router-dom";
import DownloadConstraints from "./DownloadConstraints";
import RequestNextAction, {
  requestTargetLabel,
} from "../components/RequestNextAction";
import { randomUUID } from "../randomUUID";

type Spec = components["schemas"]["RequestOptions"];
export type WantedVersion = components["schemas"]["VersionView"];
const label = (slot: string) =>
  slot === "audio" ? "Audiobook" : slot === "ebook" ? "Ebook" : "Either medium";
const stateLabel = (state: string) =>
  ({
    wanted: "Wanted",
    satisfied: "Available",
    paused: "Paused",
    cancelled: "Cancelled",
    "awaiting-inventory": "Check inventory",
  })[state] || state;

export default function Wanted({
  workId,
  version,
  clearVersion,
}: {
  workId: string;
  version: WantedVersion | null;
  clearVersion: () => void;
}) {
  const cache = useQueryClient();
  const auth = cache.getQueryData<Auth>(["session"]);
  const advanced = canRequestAdvanced(auth?.user.permissions, auth?.user.role);
  const heading = useRef<HTMLHeadingElement>(null);
  const [mode, setMode] = useState<Spec["mode"] | "">(
    version?.medium === "audio" || version?.medium === "ebook"
      ? version.medium
      : "",
  );
  const autoDownload = canAutoDownload(
    auth?.user.permissions,
    auth?.user.role,
    mode === "ebook" || mode === "audio" ? mode : undefined,
  );
  const requestEbook = canRequestMedium(
    auth?.user.permissions,
    auth?.user.role,
    "ebook",
  );
  const requestAudio = canRequestMedium(
    auth?.user.permissions,
    auth?.user.role,
    "audio",
  );
  const requestBoth = canRequestMedium(auth?.user.permissions, auth?.user.role);
  const [preferred, setPreferred] = useState<"ebook" | "audio">("ebook");
  const [preferences, setPreferences] = useState<Choice>({});
  const key = useRef(randomUUID());
  useEffect(() => {
    if (version) heading.current?.focus();
  }, [version]);
  const specification: Spec = {
    ...(mode ? { mode } : {}),
    ...(mode === "either" ? { preferred_medium: preferred } : {}),
    ...(advanced && version?.medium === "audio"
      ? { audio_version_id: version.id }
      : {}),
    ...(advanced && version?.medium === "ebook"
      ? { ebook_version_id: version.id }
      : {}),
  };
  const profiles = useQuery({
    queryKey: ["release-profiles"],
    queryFn: async () => result(await api.GET("/api/acquisition/profiles")),
  });
  const selectedProfile = profiles.data?.find(
    (p) => (p.id || "") === (preferences.profile_id || ""),
  );
  const valid = !!(
    mode ||
    preferences.overrides?.desired_media ||
    selectedProfile?.preferences.desired_media
  );
  const preview = useQuery({
    queryKey: ["request-preview", workId, specification, preferences],
    queryFn: async () =>
      result(
        await api.POST("/api/requests/preview", {
          body: {
            work_id: workId,
            specification,
            release_preferences: preferences,
          },
        }),
      ),
    enabled: valid,
  });
  const requests = usePagedQuery({
    queryKey: ["requests", workId],
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/requests", {
          signal,
          params: { query: { work_id: workId, offset, limit: 10, mine: true } },
        }),
      ),
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.total ? count : undefined;
    },
  });
  const refresh = async () => {
    await Promise.all(
      [
        "requests",
        "request-preview",
        "activity",
        "book-sources",
        "source-request",
        "quick-add",
      ].map((name) => cache.invalidateQueries({ queryKey: [name] })),
    );
  };
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/requests", {
          body: {
            work_id: workId,
            specification,
            ...(advanced
              ? {
                  release_preferences: preferences,
                  expected_preference_revision:
                    preview.data?.release_policy?.effective_revision,
                }
              : {}),
          },
          params: { header: { "idempotency-key": key.current } },
        }),
      ),
    onSuccess: async () => {
      key.current = randomUUID();
      await refresh();
    },
  });
  const cancel = useMutation({
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
  const changed = () => {
    key.current = randomUUID();
    save.reset();
  };
  return (
    <section className="panel editor library-access" aria-label="Wanted media">
      <QuotaSummary />
      <h2 ref={heading} tabIndex={-1}>
        Wanted media
      </h2>
      <p className="muted">
        {autoDownload
          ? "Track the media you want and skip copies already in your library. Save a request, then choose a source to acquire missing media."
          : "Ask for a book without starting the download. Someone with approval access can accept it into the library, or decline it."}
      </p>
      {version ? (
        <div className="source-attribution">
          <span>
            <strong>{label(version.medium)} · Selected version</strong>
            <small>
              {version.narrators.join(", ") ||
                version.title ||
                "Selected edition"}
            </small>
          </span>
          <button type="button" onClick={clearVersion}>
            Choose any acceptable version
          </button>
        </div>
      ) : (
        <label>
          Media to request
          <select
            value={mode || ""}
            onChange={(event) => {
              changed();
              setMode(event.target.value as Spec["mode"]);
            }}
          >
            <option value="">Use my download settings</option>
            {requestEbook && <option value="ebook">Ebook</option>}
            {requestAudio && <option value="audio">Audiobook</option>}
            {requestBoth && <option value="both">Both</option>}
            {requestBoth && <option value="either">Either</option>}
          </select>
        </label>
      )}
      {mode === "either" && (
        <label>
          Search first when both are missing
          <select
            value={preferred}
            onChange={(event) => {
              changed();
              setPreferred(event.target.value as "ebook" | "audio");
            }}
          >
            <option value="ebook">Ebook</option>
            <option value="audio">Audiobook</option>
          </select>
          <small>
            Either existing medium satisfies this request. Compatible pending
            requests are reused first.
          </small>
        </label>
      )}
      {advanced ? (
        <RequestPreferences
          value={preferences}
          onChange={(value) => {
            changed();
            setPreferences(value);
          }}
        />
      ) : (
        version && (
          <p className="muted">
            This account requests any acceptable version. An approver can choose
            a specific edition later.
          </p>
        )
      )}
      <Notice
        error={preview.error || save.error || requests.error || cancel.error}
      />
      {valid && preview.isPending && (
        <p className="muted">Checking your library…</p>
      )}
      {valid && preview.data && (
        <div aria-label="Request preview">
          {!!preview.data.existing_copies?.length && (
            <aside className="notice" aria-label="Possible existing copies">
              <strong>
                Check these existing library copies before requesting another.
              </strong>
              <p>
                They are displayed with this book, but their identities have not
                been confirmed as the same book.
              </p>
              {preview.data.existing_copies.map((copy) => (
                <p key={copy.asset_id}>
                  <Link
                    to={`/books/${copy.work_id}?tab=library&format=${copy.medium}`}
                  >
                    {copy.title} · {label(copy.medium)}
                  </Link>
                  {copy.narrators.length > 0 && (
                    <> · {copy.narrators.join(", ")}</>
                  )}
                  <br />
                  {copy.meets_requirements
                    ? "Matches the edition requirements"
                    : "Different or incomplete edition information"}
                  {copy.state === "stale" && " · Last known availability"}
                </p>
              ))}
            </aside>
          )}
          <EffectiveScope
            specification={preview.data.specification}
            origins={preview.data.release_policy?.scope_origins}
          />
          {preview.data.release_policy && (
            <EffectivePreferences
              preferences={preview.data.release_policy.preferences}
              origins={preview.data.release_policy.origins || {}}
            />
          )}
          {preview.data.targets.map((target) => (
            <p key={target.slot}>
              <strong>
                {label(target.slot)} · {stateLabel(target.state)}
              </strong>
              <br />
              <span className="muted">{target.message}</span>
            </p>
          ))}
          {preview.data.series_scope &&
            preview.data.series_scope.state !== "single" && (
              <p>
                {preview.data.series_scope.message}. Complete this request from
                the reviewed series page, or choose Just this book in download
                preferences.
                {preview.data.series_scope.external_id && (
                  <>
                    <br />
                    <Link
                      to={`/series/hardcover/${preview.data.series_scope.external_id}`}
                    >
                      Open reviewed series
                    </Link>
                  </>
                )}
              </p>
            )}
        </div>
      )}
      <button
        className="primary"
        type="button"
        onClick={() => save.mutate()}
        disabled={
          !valid ||
          !preview.data ||
          preview.isFetching ||
          save.isPending ||
          Boolean(
            preview.data.series_scope &&
            preview.data.series_scope.state !== "single",
          )
        }
      >
        {autoDownload ? "Save to wanted" : "Request book"}
      </button>
      {save.isSuccess && (
        <p className="success" role="status">
          {save.data.request.approval_status === "pending"
            ? "Request sent. It will download after approval."
            : "Your media request was saved."}
        </p>
      )}
      {!!requests.data?.items.length && (
        <>
          <h3>Your saved requests</h3>
          {requests.data.items.map((intent) => (
            <article className="panel editor" key={intent.id}>
              <p className="muted">{intent.description}</p>
              <EffectiveScope
                specification={intent.specification}
                origins={intent.release_policy?.scope_origins}
              />
              {intent.release_policy && (
                <EffectivePreferences
                  preferences={intent.release_policy.preferences}
                  origins={intent.release_policy.origins || {}}
                />
              )}
              <DownloadConstraints
                value={intent.specification.download_constraints}
              />
              {intent.targets.map((target) => (
                <p key={target.slot}>
                  <strong>
                    {label(target.slot)} · {requestTargetLabel(target)}
                  </strong>
                  <br />
                  <span className="muted">{target.message}</span>
                  <br />
                  <RequestNextAction request={intent} target={target} />
                </p>
              ))}
              {intent.reasons.map((reason) => (
                <div className="source-attribution" key={reason.id}>
                  <span>
                    {reason.label}
                    {reason.active ? "" : " · Cancelled"}
                  </span>
                  {intent.can_withdraw && reason.active && (
                    <button
                      type="button"
                      disabled={cancel.isPending}
                      onClick={() =>
                        cancel.mutate({ intent: intent.id, reason: reason.id })
                      }
                    >
                      Cancel {reason.label.toLowerCase()}
                    </button>
                  )}
                </div>
              ))}
            </article>
          ))}
          <InfiniteScroll query={requests} />
        </>
      )}
    </section>
  );
}
