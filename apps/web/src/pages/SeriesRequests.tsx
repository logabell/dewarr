import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "../components/InfiniteScroll";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, result, type Auth } from "../api/client";
import { canRequestMedium, canStartDownload } from "../permissions";
import type { components } from "../api/schema";
import { Notice } from "../components";
import RequestPreferences, { type Choice } from "./RequestPreferences";
import { EffectiveScope } from "./ScopeFields";
import { EffectivePreferences } from "./PreferenceFields";
import type { MainBookReview } from "./SeriesScopeReview";
import SeriesAutomaticRoutes, {
  useSeriesRoutes,
} from "./SeriesAutomaticRoutes";
import { randomUUID } from "../randomUUID";

type Spec = components["schemas"]["RequestOptions"];
type Preview = components["schemas"]["SeriesRequestView"];
const states: Record<string, string> = {
  satisfied: "Available",
  wanted: "Missing",
  pending: "Already requested",
  "awaiting-inventory": "Check inventory",
  paused: "Waiting for approval",
  cancelled: "Cancelled",
};

export default function SeriesRequests({
  externalId,
  generation,
  selected,
  mainBookReview,
  scopeReview,
}: {
  externalId: string;
  generation: number;
  selected: string[];
  mainBookReview?: MainBookReview;
  scopeReview?: ReactNode;
}) {
  const cache = useQueryClient();
  const [params, setParams] = useSearchParams();
  const id = params.get("request");
  const setId = (value: string | null) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        if (value) next.set("request", value);
        else next.delete("request");
        return next;
      },
      { replace: true },
    );
  const [scope, setScope] = useState<"selected" | "complete_series">(
    "selected",
  );
  const [confirmed, setConfirmed] = useState(false);
  const [useMainBookReview, setUseMainBookReview] = useState(false);
  const matchingReview =
    mainBookReview?.state === "current" &&
    mainBookReview.books.length === selected.length &&
    mainBookReview.books.every((book) => selected.includes(book.work_id));
  const scopeReviewId =
    scope === "complete_series" && useMainBookReview && matchingReview
      ? mainBookReview?.id
      : undefined;
  const [spec, setSpec] = useState<Spec>({});
  const [preferences, setPreferences] = useState<Choice>({});
  const [automatic, setAutomatic] = useState(false);
  const routes = useSeriesRoutes(automatic, spec.mode, preferences);
  const session = useQuery<Auth | null>({
    queryKey: ["session"],
    enabled: false,
  });
  const requestEbook = canRequestMedium(
    session.data?.user.permissions,
    session.data?.user.role,
    "ebook",
  );
  const requestAudio = canRequestMedium(
    session.data?.user.permissions,
    session.data?.user.role,
    "audio",
  );
  const requestBoth = canRequestMedium(
    session.data?.user.permissions,
    session.data?.user.role,
  );
  const canDownloadSeries =
    canStartDownload(
      session.data?.user.permissions,
      session.data?.user.role,
      "ebook",
    ) ||
    canStartDownload(
      session.data?.user.permissions,
      session.data?.user.role,
      "audio",
    );

  const key = useRef(randomUUID());
  const panel = useRef<HTMLElement>(null);
  const path = { external_id: externalId };
  const history = usePagedQuery({
    queryKey: ["series-requests", externalId],
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/catalog/series/hardcover/{external_id}/requests", {
          signal,
          params: { path, query: { offset, limit: 10 } },
        }),
      ),
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.total ? count : undefined;
    },
  });
  const saved = useQuery({
    queryKey: ["series-request", externalId, id],
    enabled: Boolean(id),
    queryFn: async () =>
      result(
        await api.GET(
          "/api/catalog/series/hardcover/{external_id}/requests/{operation_id}",
          { params: { path: { ...path, operation_id: id! } } },
        ),
      ),
    refetchInterval: (q) =>
      ["queued", "running"].includes(q.state.data?.status || "")
        ? 1000
        : ["queued", "running"].includes(
              q.state.data?.acquisition_status || "",
            ) ||
            (q.state.data?.status === "completed" &&
              q.state.data.records.some((book) =>
                book.targets.some(
                  (target) =>
                    !["satisfied", "cancelled"].includes(target.state),
                ),
              ))
          ? 10000
          : false,
  });
  const updated = (data: Preview) => {
    cache.setQueryData(["series-request", externalId, data.id], data);
    setId(data.id);
    for (const name of ["series-requests", "requests", "activity"])
      void cache.invalidateQueries({ queryKey: [name] });
  };
  useEffect(() => {
    if (id) panel.current?.focus();
  }, [id]);
  useEffect(() => {
    if (
      saved.data?.status === "completed" ||
      saved.data?.status === "cancelled"
    ) {
      for (const name of ["series-requests", "requests", "activity"])
        void cache.invalidateQueries({ queryKey: [name] });
    }
  }, [saved.data?.status, cache]);
  const preview = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/catalog/series/hardcover/{external_id}/requests/preview",
          {
            params: { path, header: { "idempotency-key": key.current } },
            body: {
              work_ids: selected,
              specification: spec,
              release_preferences: preferences,
              scope,
              confirm_main_membership: confirmed,
              scope_review_id: scopeReviewId,
              expected_generation: generation,
              automatic: automatic ? routes.input : undefined,
            },
          },
        ),
      ),
    onSuccess: updated,
  });
  const submit = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/catalog/series/hardcover/{external_id}/requests/{operation_id}/submit",
          { params: { path: { ...path, operation_id: id! } } },
        ),
      ),
    onSuccess: updated,
  });
  const cancel = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/catalog/series/hardcover/{external_id}/requests/{operation_id}/cancel",
          { params: { path: { ...path, operation_id: id! } } },
        ),
      ),
    onSuccess: updated,
  });
  const retryAcquisition = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/catalog/series/hardcover/{external_id}/requests/{operation_id}/retry-acquisition",
          { params: { path: { ...path, operation_id: id! } } },
        ),
      ),
    onSuccess: updated,
  });
  // A changed draft gets a new key; retries of the same draft retain their key.
  const draft = JSON.stringify({
    selected,
    spec,
    preferences,
    scope,
    confirmed,
    scopeReviewId,
    generation,
    automatic: automatic ? routes.input : null,
  });
  const lastDraft = useRef(draft);
  useEffect(() => {
    if (lastDraft.current !== draft) {
      key.current = randomUUID();
      lastDraft.current = draft;
      preview.reset();
      if (saved.data?.status === "preview") setId(null);
    }
  }, [draft]);
  const value = saved.data;
  const busy =
    preview.isPending ||
    submit.isPending ||
    cancel.isPending ||
    retryAcquisition.isPending ||
    ["queued", "running"].includes(value?.status || "");
  return (
    <section
      className="panel editor series-request-composer"
      aria-label="Series requests"
      tabIndex={-1}
      ref={panel}
    >
      <div className="series-composer-heading">
        <h2>{id ? "Review request" : "Your request"}</h2>
        <span className="muted">
          {id && saved.data ? saved.data.records.length : selected.length} books
          selected
        </span>
      </div>
      <Notice
        error={
          preview.error ||
          saved.error ||
          submit.error ||
          cancel.error ||
          history.error ||
          retryAcquisition.error
        }
      />
      {!id ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            preview.mutate();
          }}
        >
          <fieldset className="editor" disabled={preview.isPending}>
            <legend className="sr-only">Request format and options</legend>
            <div
              className="series-format-choices"
              role="group"
              aria-label="Series requested media"
            >
              {[
                ["inherit", "My defaults", true],
                ["audio", "Audiobook", requestAudio],
                ["ebook", "Ebook", requestEbook],
                ["both", "Both", requestBoth],
                ["either", "Either", requestBoth],
              ]
                .filter(([, , allowed]) => allowed)
                .map(([mode, label]) => (
                  <button
                    key={String(mode)}
                    type="button"
                    aria-pressed={(spec.mode || "inherit") === mode}
                    onClick={() =>
                      setSpec({
                        ...spec,
                        mode:
                          mode === "inherit"
                            ? undefined
                            : (mode as Spec["mode"]),
                        preferred_medium: undefined,
                      })
                    }
                  >
                    {String(label)}
                  </button>
                ))}
            </div>
            {spec.mode === "either" && (
              <label>
                Series first medium
                <select
                  value={spec.preferred_medium || "inherit"}
                  onChange={(e) =>
                    setSpec({
                      ...spec,
                      preferred_medium:
                        e.target.value === "inherit"
                          ? undefined
                          : (e.target.value as "ebook" | "audio"),
                    })
                  }
                >
                  <option value="inherit">Use my defaults</option>
                  <option value="audio">Audiobook</option>
                  <option value="ebook">Ebook</option>
                </select>
              </label>
            )}

            <label className="check-label">
              <input
                type="checkbox"
                checked={automatic}
                onChange={(event) => setAutomatic(event.target.checked)}
              />
              Automatically acquire missing books after review
            </label>
            {automatic && <SeriesAutomaticRoutes selection={routes} />}
            <p className="muted series-request-hint">
              {automatic
                ? "Verified packs can serve multiple selected books in one download. Uncertain contents stay in review."
                : "Save your requests, then review sources. Collection sources open a book and file selection dialog."}
            </p>
            <details className="series-request-options">
              <summary>Request options</summary>
              <label>
                Series request scope
                <select
                  value={scope}
                  onChange={(e) => {
                    setScope(e.target.value as typeof scope);
                    setConfirmed(false);
                  }}
                >
                  <option value="selected">Selected books</option>
                  <option value="complete_series">
                    Complete reviewed main-book set
                  </option>
                </select>
              </label>
              {scope === "complete_series" && (
                <>
                  {matchingReview && (
                    <label className="check-label">
                      <input
                        type="checkbox"
                        checked={useMainBookReview}
                        onChange={(event) => {
                          setUseMainBookReview(event.target.checked);
                          setConfirmed(false);
                        }}
                      />
                      Use saved main-book review (revision{" "}
                      {mainBookReview?.revision})
                    </label>
                  )}
                  {!scopeReviewId && (
                    <label className="check-label">
                      <input
                        type="checkbox"
                        checked={confirmed}
                        onChange={(e) => setConfirmed(e.target.checked)}
                      />
                      I reviewed the selection and it contains the main books I
                      want to complete.
                    </label>
                  )}
                </>
              )}
              <RequestPreferences
                value={preferences}
                onChange={setPreferences}
              />
              {scopeReview}
            </details>
            <button
              className="primary series-review-button"
              disabled={
                !selected.length ||
                selected.length > 100 ||
                (automatic && !routes.input) ||
                (scope === "complete_series" && !confirmed && !scopeReviewId)
              }
            >
              {preview.isPending
                ? "Preparing review…"
                : `Review ${selected.length} selected book${selected.length === 1 ? "" : "s"}`}
            </button>
          </fieldset>
        </form>
      ) : value ? (
        <>
          <p role="status">{value.message}</p>
          {value.selected_pack_only && (
            <p>
              These additional reviewed books use the original selected pack and
              medium. Already available books are skipped. Missing or uncertain
              contents need review; this request does not start separate
              downloads for them.
            </p>
          )}
          {value.originating_list_id && (
            <p>
              Automatically requested by a list policy.{" "}
              <Link to={`/lists/${value.originating_list_id}`}>
                Open originating list
              </Link>
            </p>
          )}
          {value.automatic && (
            <p>
              {value.acquisition_message ||
                "After you accept, missing requested media will be acquired through the approved routes."}
            </p>
          )}
          <p className="series-request-totals">
            {value.counts.wanted} missing · {value.counts.satisfied} available ·{" "}
            {value.counts.pending} already requested
            {value.counts.cancelled > 0 &&
              ` · ${value.counts.cancelled} cancelled`}
          </p>
          {value.main_membership === "user-confirmed" && (
            <p>
              These are your reviewed main books, saved from catalog revision{" "}
              {value.catalog_generation}.
              {value.scope_review_revision &&
                ` Reused main-book review ${value.scope_review_revision}.`}
            </p>
          )}
          <details className="series-request-options">
            <summary>Saved request options</summary>
            <EffectiveScope
              specification={value.specification}
              origins={value.release_policy.scope_origins}
            />
            <EffectivePreferences
              preferences={value.release_policy.preferences}
              origins={value.release_policy.origins || {}}
            />
          </details>
          <div className="series-request-results">
            {value.records.map((book, index) => (
              <article
                className="series-request-result"
                key={`${book.work_id}:${index}`}
              >
                <h3>
                  <Link to={`/books/${book.work_id}`}>
                    {book.position != null && `${book.position} · `}
                    {book.title}
                  </Link>
                </h3>
                {book.issue && <p>{book.issue}</p>}
                {book.acquisition_message && <p>{book.acquisition_message}</p>}
                {book.next_check_at && (
                  <small>
                    Next check: {new Date(book.next_check_at).toLocaleString()}
                  </small>
                )}
                {book.warnings.map((warning) => (
                  <p className="muted" key={warning}>
                    {warning}
                  </p>
                ))}
                {book.targets.map((target) => (
                  <p key={target.slot}>
                    {target.slot === "audio"
                      ? "Audiobook"
                      : target.slot === "ebook"
                        ? "Ebook"
                        : "Either medium"}
                    : {states[target.state] || target.state}
                  </p>
                ))}
                {value.receipt?.find(
                  (receipt) => receipt.work_id === book.work_id,
                ) && (
                  <Link to={`/books/${book.work_id}?tab=sources`}>
                    Review sources
                  </Link>
                )}
              </article>
            ))}
          </div>
          {value.omitted.length > 0 && (
            <details>
              <summary>
                {value.omitted.length} books outside this request
              </summary>
              {value.omitted.map((book) => (
                <p key={book.work_id}>
                  {book.title} · {book.warnings.join(" · ") || book.reason}
                </p>
              ))}
            </details>
          )}
          <div className="button-row">
            {["preview", "failed"].includes(value.status) && (
              <button disabled={busy} onClick={() => submit.mutate()}>
                {value.accepted_at
                  ? "Retry saved series request"
                  : value.automatic
                    ? "Start automatic series acquisition"
                    : canDownloadSeries
                      ? "Save series requests"
                      : "Send series for approval"}
              </button>
            )}
            {value.can_retry_acquisition && (
              <button disabled={busy} onClick={() => retryAcquisition.mutate()}>
                Retry series acquisition
              </button>
            )}
            {value.status !== "cancelled" && (
              <button
                disabled={submit.isPending || cancel.isPending}
                onClick={() => cancel.mutate()}
              >
                Cancel this series request
              </button>
            )}
            <button
              disabled={busy}
              onClick={() => {
                setId(null);
                key.current = randomUUID();
                preview.reset();
                submit.reset();
                cancel.reset();
                retryAcquisition.reset();
                panel.current?.focus();
              }}
            >
              New selection
            </button>
          </div>
        </>
      ) : (
        <p role="status">Loading saved series request…</p>
      )}
      {!!history.data?.total && (
        <details>
          <summary>Series request history ({history.data.total})</summary>
          {history.data.items.map((item) => (
            <div className="button-row" key={item.id}>
              <button disabled={busy} onClick={() => setId(item.id)}>
                Open {item.count}-book request ·{" "}
                {new Date(item.created_at).toLocaleString()}
              </button>
              <span>{item.status}</span>
            </div>
          ))}
          <InfiniteScroll query={history} />
        </details>
      )}
    </section>
  );
}
