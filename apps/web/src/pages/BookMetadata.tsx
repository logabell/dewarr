import { Check, Image, LockKeyhole, Pencil, RefreshCw } from "lucide-react";
import { languageName } from "../components/LanguageSelect";
import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "../components/InfiniteScroll";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { Work } from "../api/client";
import type { components } from "../api/schema";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
import { providerName } from "./ProviderSearch";
import BookMatch from "./BookMatch";
import { fieldLabel } from "./MetadataSettings";
import IdentityHistory, { useRefreshIdentity } from "./IdentityHistory";
import VersionReviews from "./VersionReview";

type Edit = components["schemas"]["EditValues"];
type Field =
  | "title"
  | "authors"
  | "description"
  | "publication_year"
  | "language"
  | "cover_url";

export default function BookMetadata({
  work,
  admin,
  onWantVersion,
  toolsOnly = false,
}: {
  work: Work;
  toolsOnly?: boolean;
  admin: boolean;
  onWantVersion?: (version: components["schemas"]["VersionView"]) => void;
}) {
  const client = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [reviewingEditions, setReviewingEditions] = useState(false);
  const [unmatching, setUnmatching] = useState<
    components["schemas"]["SourceView"] | null
  >(null);
  const refreshIdentity = useRefreshIdentity();
  const metadata = usePagedQuery({
    queryKey: ["work-metadata", work.id],
    queryFn: async (offset, signal) => {
      const data = result(
        await api.GET("/api/metadata/works/{work_id}", {
          params: { path: { work_id: work.id }, query: { offset, limit: 20 } },
          signal,
        }),
      );
      return { ...data, items: data.versions };
    },
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.versions_total
        ? count
        : undefined;
    },
    refetchInterval: (query) =>
      ["queued", "running", "retrying"].includes(
        query.state.data?.pages[0]?.enrichment?.status || "",
      )
        ? 3000
        : false,
  });
  const enrichmentId = metadata.data?.enrichment?.id;
  const enrichmentStatus = metadata.data?.enrichment?.status;
  useEffect(() => {
    if (enrichmentStatus === "completed") {
      client.invalidateQueries({ queryKey: ["work", work.id] });
      client.invalidateQueries({ queryKey: ["works"] });
    }
  }, [client, work.id, enrichmentId, enrichmentStatus]);
  const enrich = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/metadata/works/{work_id}/enrichment", {
          params: { path: { work_id: work.id } },
        }),
      ),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: ["work-metadata", work.id] }),
  });
  const updated = () => {
    client.invalidateQueries({ queryKey: ["work", work.id] });
    client.invalidateQueries({ queryKey: ["work-reader-match", work.id] });
    client.invalidateQueries({ queryKey: ["library-groups"] });
    client.invalidateQueries({ queryKey: ["work-metadata", work.id] });
    client.invalidateQueries({ queryKey: ["reader-work-metadata", work.id] });
    client.invalidateQueries({ queryKey: ["works"] });
    client.invalidateQueries({ queryKey: ["version-reviews", work.id] });
    client.invalidateQueries({ queryKey: ["identity-history"] });
  };
  const refresh = useMutation({
    mutationFn: async (source: {
      provider: components["schemas"]["BookData"]["provider"];
      external_id: string;
    }) =>
      result(
        await api.POST("/api/metadata/works/{work_id}/source", {
          params: { path: { work_id: work.id } },
          body: { ...source, confirm_match: false },
        }),
      ),
    onSuccess: updated,
  });
  const edit = useMutation({
    mutationFn: async (body: { values?: Edit; unlock?: Field[] }) =>
      result(
        await api.PATCH("/api/metadata/works/{work_id}", {
          params: { path: { work_id: work.id } },
          body,
        }),
      ),
    onSuccess: () => {
      updated();
      setEditing(false);
    },
  });
  const more = useMutation({
    mutationFn: async (source: {
      provider: components["schemas"]["BookData"]["provider"];
      external_id: string;
    }) =>
      result(
        await api.POST("/api/metadata/works/{work_id}/source/editions", {
          params: { path: { work_id: work.id } },
          body: { ...source, confirm_match: false },
        }),
      ),
    onSuccess: updated,
  });
  const unmatch = useMutation({
    mutationFn: async () => {
      if (!unmatching?.revision)
        throw new Error("Refresh the book before correcting its source.");
      return result(
        await api.POST("/api/identity/sources/{source_id}/unmatch", {
          params: { path: { source_id: unmatching.id } },
          body: { expected_revision: unmatching.revision },
        }),
      );
    },
    onSuccess: async () => {
      setUnmatching(null);
      await refreshIdentity();
    },
  });
  return (
    <section
      className="library-access metadata-workflow"
      aria-label="Manage book metadata"
    >
      <header className="metadata-page-heading">
        <div>
          <h2>Book metadata</h2>
          <p className="muted">
            Keep the right match, details and cover for this book.
          </p>
        </div>
        {!admin && <span className="status">Read-only</span>}
      </header>
      {metadata.data && (
        <BookMatch
          key={work.id}
          work={work}
          admin={admin}
          source={metadata.data.sources.find(
            (source) => source.provider === "hardcover",
          )}
          updated={updated}
        />
      )}
      <section
        className="metadata-card"
        aria-labelledby="metadata-details-heading"
      >
        <div className="metadata-edit-row">
          <div>
            <h3 id="metadata-details-heading">Book details</h3>
            <p className="muted">
              Your edits stay protected when catalog details refresh.
            </p>
          </div>
          {admin && (
            <button
              disabled={edit.isPending}
              onClick={() => {
                edit.reset();
                setEditing(!editing);
              }}
              aria-expanded={editing}
              aria-controls="metadata-details-content"
            >
              <Pencil size={14} /> {editing ? "Close editor" : "Edit details"}
            </button>
          )}
        </div>
        <div id="metadata-details-content">
          {editing ? (
            <EditForm
              work={work}
              pending={edit.isPending}
              cancel={() => {
                edit.reset();
                setEditing(false);
              }}
              save={(values) => edit.mutate({ values })}
            />
          ) : (
            <>
              <dl className="metadata-details-grid">
                {(
                  [
                    ["title", "Title", work.title],
                    [
                      "authors",
                      "Authors",
                      work.authors.join(", ") || "Not supplied",
                    ],
                    [
                      "publication_year",
                      "Published",
                      work.publication_year ?? "Not supplied",
                    ],
                    [
                      "language",
                      "Language",
                      languageName(work.language) || "Not supplied",
                    ],
                  ] as const
                ).map(([field, label, value]) => (
                  <div key={field}>
                    <dt>
                      {label}
                      {(
                        metadata.data?.fields[field] as
                          { locked?: boolean } | undefined
                      )?.locked && (
                        <span className="metadata-protected">
                          <LockKeyhole size={11} /> Protected
                        </span>
                      )}
                    </dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
              <details className="metadata-description">
                <summary>
                  Description{" "}
                  {(
                    metadata.data?.fields.description as
                      { locked?: boolean } | undefined
                  )?.locked && (
                    <span className="metadata-protected">
                      <LockKeyhole size={11} /> Protected
                    </span>
                  )}
                </summary>
                <p className="reader-prose">
                  {work.description ||
                    (admin
                      ? "No description yet. Edit details to add one."
                      : "No description available.")}
                </p>
              </details>
            </>
          )}
        </div>
      </section>
      {edit.isSuccess && (
        <p className="metadata-feedback success" role="status">
          <Check size={16} /> Book details updated.
        </p>
      )}
      {refresh.isSuccess && (
        <p className="metadata-feedback success" role="status">
          <Check size={16} /> Catalog details refreshed.
        </p>
      )}
      <Notice
        error={
          metadata.error ||
          refresh.error ||
          edit.error ||
          more.error ||
          unmatch.error ||
          enrich.error
        }
      />
      {unmatching && (
        <section className="panel editor" aria-label="Remove catalog source">
          <h3>
            Stop using {providerName(unmatching.provider)} for this match?
          </h3>
          <p>{unmatching.title}</p>
          <p className="muted">
            This source will stop supplying metadata and catalog-only editions.
            Your book's label, protected edits and library copies remain.
            Correction history can undo this decision.
          </p>
          <div className="button-row">
            <button
              type="button"
              disabled={unmatch.isPending}
              onClick={() => unmatch.mutate()}
            >
              Remove catalog match
            </button>
            <button type="button" onClick={() => setUnmatching(null)}>
              Cancel removal
            </button>
          </div>
        </section>
      )}
      {metadata.isPending && <Loading />}
      {metadata.data && (
        <>
          {!toolsOnly && metadata.data.enrichment && (
            <div
              className="source-attribution"
              aria-label="Automatic metadata lookup"
            >
              <span>
                <strong>Automatic metadata</strong>
                <small>{metadata.data.enrichment.message}</small>
              </span>
              {admin && metadata.data.enrichment_retryable && (
                <button
                  type="button"
                  disabled={enrich.isPending}
                  onClick={() => enrich.mutate()}
                >
                  Check missing details again
                </button>
              )}
            </div>
          )}
          {!toolsOnly && (
            <>
              {metadata.data.items.length === 0 && (
                <p className="muted">
                  No catalog editions have been linked yet. Library copies
                  appear below.
                </p>
              )}
              <div className="edition-grid">
                {metadata.data.items.map((version) => (
                  <article className="panel edition-card" key={version.id}>
                    <div className="section-heading">
                      <strong>
                        {version.medium === "audio"
                          ? "Audiobook"
                          : version.medium === "ebook"
                            ? "Ebook"
                            : version.medium === "print"
                              ? "Print edition"
                              : "Format unknown"}
                      </strong>
                      {version.owned && (
                        <span className="status owned">In library</span>
                      )}
                    </div>
                    <h3>{version.title || work.title}</h3>
                    <p>
                      {version.runtime_minutes
                        ? `${version.runtime_minutes} minutes · `
                        : ""}
                      {version.narrators.length
                        ? `Narrated by ${version.narrators.join(", ")}`
                        : version.medium === "audio"
                          ? "Narrator unknown"
                          : ""}
                    </p>
                    <p className="muted">
                      {[version.language, version.publication_year]
                        .filter(Boolean)
                        .join(" · ") || "Edition details unavailable"}
                    </p>
                    {version.needs_review && (
                      <p className="notice">
                        Provider details changed. The existing version was
                        preserved for review.
                      </p>
                    )}
                    {onWantVersion &&
                      ["ebook", "audio"].includes(version.medium) && (
                        <button
                          type="button"
                          onClick={() => onWantVersion(version)}
                        >
                          Request this{" "}
                          {version.medium === "audio" ? "recording" : "edition"}
                        </button>
                      )}
                  </article>
                ))}
              </div>
              <InfiniteScroll query={metadata} />
              {metadata.data.sources.some((source) => source.editions_more) && (
                <p className="muted">
                  This catalog contains more editions than have been loaded.
                  Listed editions are not a complete catalog.
                </p>
              )}
            </>
          )}
          <section
            className="metadata-card metadata-covers"
            aria-labelledby="metadata-covers-heading"
          >
            <div className="metadata-card-heading">
              <div>
                <h3 id="metadata-covers-heading">Book cover</h3>
                <p className="muted">
                  {admin
                    ? "Choose the cover you want to see in your library."
                    : "The cover used for this book in your library."}
                </p>
              </div>
              <Image size={18} aria-hidden="true" />
            </div>
            <div className="metadata-cover-options">
              {[
                ...new Set(
                  [...metadata.data.cover_choices, work.cover_url].filter(
                    (cover): cover is string => !!cover,
                  ),
                ),
              ].map((cover, index) => (
                <button
                  key={cover}
                  className="metadata-cover-option"
                  aria-label={`Use cover ${index + 1}`}
                  aria-pressed={work.cover_url === cover}
                  disabled={!admin || edit.isPending}
                  onClick={() => edit.mutate({ values: { cover_url: cover } })}
                >
                  <img
                    src={cover}
                    alt={`Cover option ${index + 1}`}
                    loading="lazy"
                    referrerPolicy="no-referrer"
                  />
                  <span>
                    {work.cover_url === cover ? (
                      <>
                        <Check size={12} /> Current
                      </>
                    ) : (
                      `Cover ${index + 1}`
                    )}
                  </span>
                </button>
              ))}
              {!work.cover_url && !metadata.data.cover_choices.length && (
                <p className="muted">
                  No covers available. Connect a catalog match or refresh its
                  details to look for a cover.
                </p>
              )}
            </div>
            {admin && work.cover_url && (
              <button
                disabled={edit.isPending}
                onClick={() => edit.mutate({ values: { cover_url: null } })}
              >
                Hide cover
              </button>
            )}
          </section>
          <details className="catalog-source-tools">
            <summary>Advanced metadata options</summary>
            {admin && (
              <details
                onToggle={(event) =>
                  setReviewingEditions(event.currentTarget.open)
                }
              >
                <summary>Edition matching</summary>
                {reviewingEditions && <VersionReviews workId={work.id} />}
              </details>
            )}
            {metadata.data.sources.map((source) => (
              <div
                className="source-attribution"
                key={`${source.provider}:${source.external_id}`}
              >
                <span>
                  <strong>{providerName(source.provider)}</strong> ·{" "}
                  {source.title}
                  <small>
                    Fetched {new Date(source.fetched_at).toLocaleString()}
                  </small>
                  {source.series.map((series) => (
                    <small key={series.external_id}>
                      {source.provider === "hardcover" ? (
                        <Link to={`/series/hardcover/${series.external_id}`}>
                          {series.name}
                        </Link>
                      ) : (
                        series.name
                      )}
                      {series.position ? ` · Book ${series.position}` : ""}
                    </small>
                  ))}
                </span>
                {admin && (
                  <button
                    disabled={refresh.isPending || more.isPending}
                    onClick={() => refresh.mutate(source)}
                  >
                    <RefreshCw size={14} /> Refresh{" "}
                    {providerName(source.provider)}
                  </button>
                )}
                {admin && source.editions_more && (
                  <button
                    disabled={more.isPending || refresh.isPending}
                    onClick={() => more.mutate(source)}
                  >
                    {more.isPending
                      ? "Loading editions…"
                      : `Load more ${providerName(source.provider)} editions`}
                  </button>
                )}
                {admin && (
                  <button
                    type="button"
                    onClick={() => {
                      unmatch.reset();
                      setUnmatching(source);
                    }}
                  >
                    Unmatch {providerName(source.provider)}
                  </button>
                )}
              </div>
            ))}
            <details className="panel provenance">
              <summary>Metadata sources and protected edits</summary>
              {Object.entries(metadata.data.fields).length === 0 && (
                <p className="muted">No provider metadata has been selected.</p>
              )}
              {Object.entries(metadata.data.fields).map(([field, raw]) => {
                const value = raw as {
                  provider?: string;
                  reason?: string;
                  locked?: boolean;
                };
                return (
                  <div className="source-attribution" key={field}>
                    <span>
                      <strong>{fieldLabel(field)}</strong>
                      <small>
                        {value.provider === "unmatched"
                          ? "Unmatched source"
                          : value.provider === "manual"
                            ? "Your edit"
                            : providerName(value.provider || "")}
                        {value.locked ? " · Protected" : ""}
                      </small>
                      <small>{value.reason}</small>
                    </span>
                    {admin && value.locked && (
                      <button
                        onClick={() =>
                          edit.mutate({ unlock: [field as Field] })
                        }
                        disabled={edit.isPending}
                      >
                        Use provider {fieldLabel(field).toLowerCase()}
                      </button>
                    )}
                  </div>
                );
              })}
            </details>
            {admin && <IdentityHistory workId={work.id} />}
          </details>
        </>
      )}
    </section>
  );
}

function EditForm({
  work,
  pending,
  save,
  cancel,
}: {
  cancel: () => void;
  work: Work;
  pending: boolean;
  save: (values: Edit) => void;
}) {
  return (
    <form
      className="editor metadata-editor"
      onSubmit={(event) => {
        event.preventDefault();
        const form = new FormData(event.currentTarget);
        const proposed: Edit = {
          title: String(form.get("title")),
          authors: String(form.get("authors"))
            .split("\n")
            .map((s) => s.trim())
            .filter(Boolean),
          description: String(form.get("description")) || null,
          publication_year: form.get("year") ? Number(form.get("year")) : null,
          language: String(form.get("language")) || null,
        };
        const changed = Object.fromEntries(
          Object.entries(proposed).filter(
            ([key, value]) =>
              JSON.stringify(value) !== JSON.stringify(work[key as keyof Work]),
          ),
        ) as Edit;
        save(changed);
      }}
    >
      <p className="muted">
        Changed fields are protected from future provider refreshes. Your
        library files and Audiobookshelf metadata stay unchanged.
      </p>
      <div className="metadata-editor-grid">
        <label>
          Book title
          <input
            name="title"
            autoFocus
            defaultValue={work.title}
            required
            maxLength={600}
          />
        </label>
        <label>
          Authors, one per line
          <textarea
            name="authors"
            defaultValue={work.authors.join("\n")}
            rows={3}
          />
        </label>
        <label className="metadata-editor-description">
          Book description
          <textarea
            name="description"
            defaultValue={work.description || ""}
            rows={5}
            maxLength={30000}
          />
        </label>
        <div className="form-row">
          <label>
            Publication year
            <input
              name="year"
              type="number"
              min={0}
              max={9999}
              defaultValue={work.publication_year ?? ""}
            />
          </label>
          <label>
            Book language
            <input
              name="language"
              defaultValue={work.language || ""}
              maxLength={20}
            />
          </label>
        </div>
      </div>
      <div className="metadata-editor-footer">
        <span className="muted">
          <LockKeyhole size={13} /> Only changed fields are protected.
        </span>
        <div className="button-row">
          <button type="button" disabled={pending} onClick={cancel}>
            Cancel
          </button>
          <button className="primary" disabled={pending}>
            {pending ? "Saving…" : "Save protected edits"}
          </button>
        </div>
      </div>
    </form>
  );
}
