import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import DownloadImportReview from "../components/DownloadImportReview";
import ImportExecution from "../components/ImportExecution";
import GroupingEditor from "../components/GroupingEditor";
import ImportCollectionContents from "../components/ImportCollectionContents";
import { randomUUID } from "../randomUUID";

type Group = components["schemas"]["InspectedGroup"];
type Selection = components["schemas"]["GroupSelection"];
type Inspection = components["schemas"]["InspectionView"];
type CatalogMatch = components["schemas"]["GroupMatch"];

function matchedSelection(match: CatalogMatch): Selection | null {
  const candidate = match.candidates.find(
    (item) => item.version_id === match.selected_version_id,
  );
  return match.status === "matched" && candidate
    ? {
        group_key: match.group_key,
        work_id: candidate.work_id,
        version_id: candidate.version_id,
        match_revision: match.revision,
        full_content: false,
        contents_confirmed: false,
      }
    : null;
}

export default function ImportReview() {
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("inspection");
  const [source, setSource] = useState("");
  const [path, setPath] = useState("");
  const [complete, setComplete] = useState(false);
  const [manualReview, setManualReview] = useState(false);
  const [offset, setOffset] = useState(0);
  const cache = useQueryClient();
  const attempt = useRef<{ payload: string; key: string } | null>(null);
  const roots = useQuery({
    queryKey: ["download-roots"],
    queryFn: async () =>
      result(await api.GET("/api/organization/download-roots")),
  });
  const history = useQuery({
    queryKey: ["inspections", offset],
    queryFn: async () =>
      result(
        await api.GET("/api/organization/inspections", {
          params: { query: { offset } },
        }),
      ),
    refetchInterval: (query) =>
      query.state.data?.some((row) => ["queued", "running"].includes(row.state))
        ? 2000
        : false,
  });
  const selected = useQuery({
    queryKey: ["inspection", selectedId],
    enabled: !!selectedId,
    queryFn: async () =>
      result(
        await api.GET("/api/organization/inspections/{inspection_id}", {
          params: { path: { inspection_id: selectedId! } },
        }),
      ),
    refetchInterval: (query) =>
      query.state.data &&
      (["queued", "running"].includes(query.state.data.state) ||
        (query.state.data.download &&
          !["complete", "cancelled"].includes(query.state.data.download.state)))
        ? query.state.data.download?.state === "held"
          ? 10000
          : 2000
        : false,
  });
  const create = useMutation({
    mutationFn: async () => {
      const body = {
        source_key: source || roots.data?.[0] || "",
        relative_path: path,
        completed_download: true as const,
      };
      const payload = JSON.stringify(body);
      if (attempt.current?.payload !== payload)
        attempt.current = { payload, key: randomUUID() };
      return result(
        await api.POST("/api/organization/inspections", {
          body,
          params: { header: { "idempotency-key": attempt.current.key } },
        }),
      );
    },
    onSuccess: (row) => {
      attempt.current = null;
      setParams({ inspection: row.id });
      cache.setQueryData(["inspection", row.id], row);
      cache.invalidateQueries({ queryKey: ["inspections"] });
    },
  });
  return (
    <div className="import-review-page">
      <Link className="import-review-back" to="/requests">
        ← Requests
      </Link>
      <div className="page-heading">
        <div>
          <p className="eyebrow">Downloads</p>
          <h1>{selectedId ? "Download review" : "Completed downloads"}</h1>
          <p className="muted">
            {selectedId
              ? "Your book, from download to library."
              : "Review downloads that need your attention, or import a local folder."}
          </p>
        </div>
      </div>
      <Notice error={selected.error} />
      {selectedId && selected.isPending && <Loading />}
      {selected.data?.download ? (
        <>
          <DownloadImportReview inspection={selected.data} />
          {(!selected.data.plan_id ||
            selected.data.download.state === "cancelled") && (
            <details
              className="import-review-advanced"
              onToggle={(event) => setManualReview(event.currentTarget.open)}
            >
              <summary>Change book or file selection</summary>
              {manualReview && (
                <Review key={selected.data.id} inspection={selected.data} />
              )}
            </details>
          )}
        </>
      ) : selected.data ? (
        <Review key={selected.data.id} inspection={selected.data} />
      ) : null}
      <details className="import-review-advanced" open={!selectedId}>
        <summary>Import another download</summary>
        <form
          className="panel editor"
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate();
          }}
        >
          <h2>Inspect a download</h2>
          {!roots.data?.length && !roots.isPending && (
            <p className="notice">
              No download roots are configured. Configure read-only worker
              mounts and BOOK_IMPORT_SOURCES before inspecting files.
            </p>
          )}
          <label>
            Download root
            <select
              value={source || roots.data?.[0] || ""}
              onChange={(event) => setSource(event.target.value)}
              disabled={create.isPending}
            >
              {!roots.data?.length && (
                <option value="">No configured roots</option>
              )}
              {roots.data?.map((key) => (
                <option value={key} key={key}>
                  {key}
                </option>
              ))}
            </select>
          </label>
          <label>
            Download path
            <input
              value={path}
              onChange={(event) => setPath(event.target.value)}
              placeholder="Series pack folder or completed-book.epub"
              maxLength={1024}
              required
              disabled={create.isPending}
            />
          </label>
          <p className="muted">
            Enter a completed file or folder relative to the selected root.
          </p>
          <label className="check-label">
            <input
              type="checkbox"
              checked={complete}
              onChange={(event) => setComplete(event.target.checked)}
              disabled={create.isPending}
            />
            The download has finished and its files are no longer changing
          </label>
          <Notice error={roots.error || create.error} />
          <button
            className="primary"
            disabled={
              !complete || !path || !roots.data?.length || create.isPending
            }
          >
            {create.isPending ? "Queuing…" : "Inspect files"}
          </button>
        </form>
        <section className="panel editor" aria-label="Inspection history">
          <h2>Recent inspections</h2>
          <Notice error={history.error} />
          {history.data?.map((row) => (
            <div className="import-path" key={row.id}>
              <button onClick={() => setParams({ inspection: row.id })}>
                {row.relative_path} · {row.state}
              </button>
              <span className="muted">{row.message}</span>
            </div>
          ))}
          {history.data?.length === 0 && (
            <p className="muted">No inspections yet.</p>
          )}
          <div className="actions">
            <button
              disabled={offset === 0}
              onClick={() => setOffset((value) => Math.max(0, value - 25))}
            >
              Previous inspections
            </button>
            <button
              disabled={(history.data?.length || 0) < 25}
              onClick={() => setOffset((value) => value + 25)}
            >
              More inspections
            </button>
          </div>
        </section>
      </details>
    </div>
  );
}

function Review({ inspection }: { inspection: Inspection }) {
  const cache = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [selections, setSelections] = useState<Record<string, Selection>>({});
  const [groupOffset, setGroupOffset] = useState(0);
  const [fileLimit, setFileLimit] = useState(100);
  const [editingGroups, setEditingGroups] = useState(false);
  const [includeCovers, setIncludeCovers] = useState(true);
  const snapshot = inspection.snapshot;
  const grouping = useQuery({
    queryKey: ["inspection-grouping", inspection.id],
    enabled: !!snapshot && inspection.state === "ready",
    queryFn: async () =>
      result(
        await api.GET(
          "/api/organization/inspections/{inspection_id}/grouping",
          {
            params: { path: { inspection_id: inspection.id } },
          },
        ),
      ),
  });
  const groups = grouping.data?.content.groups || [];
  const matches = useQuery({
    queryKey: [
      "inspection-matches",
      inspection.id,
      grouping.data?.revision,
      groupOffset,
    ],
    enabled: !!grouping.data && inspection.state === "ready" && !editingGroups,
    queryFn: async () =>
      result(
        await api.GET("/api/organization/inspections/{inspection_id}/matches", {
          params: {
            path: { inspection_id: inspection.id },
            query: {
              grouping_revision: grouping.data!.revision,
              offset: groupOffset,
              limit: 10,
            },
          },
        }),
      ),
  });
  const clearMatches = (matches.data?.items || [])
    .map(matchedSelection)
    .filter((item): item is Selection => !!item && !selections[item.group_key]);
  const planId = params.get("plan") || inspection.plan_id;
  const settings = useQuery({
    queryKey: ["naming-review-settings"],
    queryFn: async () => result(await api.GET("/api/organization/settings")),
  });
  const frozen = useQuery({
    queryKey: ["frozen-import-plan", planId],
    enabled: !!planId,
    queryFn: async () =>
      result(
        await api.GET("/api/organization/plans/{plan_id}", {
          params: { path: { plan_id: planId! } },
        }),
      ),
  });
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/organization/inspections/{inspection_id}/plans", {
          params: { path: { inspection_id: inspection.id } },
          body: {
            inspection_revision: snapshot!.revision,
            profile_revision: settings.data!.revision,
            grouping_revision: grouping.data!.revision,
            include_covers: includeCovers,
            selections: Object.values(selections),
          },
        }),
      ),
    onSuccess: (plan) =>
      setParams({ inspection: inspection.id, plan: plan.id }),
  });
  return (
    <section aria-label="Inspected download" className="library-access">
      <h2>{inspection.relative_path}</h2>
      {inspection.state !== "ready" && (
        <p role="status">{inspection.message}</p>
      )}
      {snapshot && (
        <>
          <p className="muted">
            {snapshot.files.length}{" "}
            {snapshot.files.length === 1 ? "file" : "files"} inspected ·{" "}
            {groups.length} book {groups.length === 1 ? "group" : "groups"}.
          </p>
          {snapshot.source_kind === "file" && (
            <p className="muted">
              Only the selected file is included. Other downloads in its folder
              are untouched.
            </p>
          )}
          <Notice error={grouping.error} />
          {grouping.isPending && <Loading />}
          {grouping.data && !editingGroups && (
            <button
              disabled={save.isPending}
              onClick={() => setEditingGroups(true)}
            >
              Review file groups
            </button>
          )}
          {editingGroups && grouping.data && (
            <GroupingEditor
              key={grouping.data.revision}
              inspection={inspection}
              grouping={grouping.data}
              onCancel={() => setEditingGroups(false)}
              onSave={(value) => {
                cache.setQueryData(
                  ["inspection-grouping", inspection.id],
                  value,
                );
                setSelections({});
                setGroupOffset(0);
                setEditingGroups(false);
                setParams({ inspection: inspection.id });
              }}
            />
          )}
          {grouping.data?.content.excluded.length ? (
            <p className="muted">
              {grouping.data.content.excluded.length} extra files excluded from
              the library copy.
            </p>
          ) : null}
          {!editingGroups && grouping.data && (
            <section aria-label="Catalog matching">
              <Notice error={matches.error} />
              {matches.isFetching && (
                <p role="status">Checking catalog matches…</p>
              )}
              <div className="actions">
                {clearMatches.length > 0 && (
                  <button
                    disabled={
                      save.isPending ||
                      matches.isFetching ||
                      !clearMatches.length
                    }
                    onClick={() =>
                      setSelections((current) => {
                        const next = { ...current };
                        for (const selection of clearMatches) {
                          if (!next[selection.group_key])
                            next[selection.group_key] = selection;
                        }
                        return next;
                      })
                    }
                  >
                    Use matched books ({clearMatches.length})
                  </button>
                )}
                <button
                  disabled={save.isPending || matches.isFetching}
                  onClick={() => matches.refetch()}
                >
                  Refresh catalog matches
                </button>
              </div>
            </section>
          )}
          {!editingGroups &&
            groups.slice(groupOffset, groupOffset + 10).map((group) => (
              <GroupMatch
                key={`${grouping.data?.revision}:${group.key}`}
                linkedWorkId={
                  groups.length === 1 ? inspection.download?.work_id : undefined
                }
                inspectionId={inspection.id}
                groupingRevision={grouping.data?.revision || ""}
                group={group}
                match={matches.data?.items.find(
                  (item) => item.group_key === group.key,
                )}
                selection={selections[group.key]}
                disabled={save.isPending}
                onChange={(selection) =>
                  setSelections((current) => {
                    const next = { ...current };
                    if (selection) next[group.key] = selection;
                    else delete next[group.key];
                    return next;
                  })
                }
              />
            ))}
          {groups.length > 10 && (
            <div className="actions">
              <button
                disabled={groupOffset === 0}
                onClick={() =>
                  setGroupOffset((value) => Math.max(0, value - 10))
                }
              >
                Previous groups
              </button>
              <button
                disabled={groupOffset + 10 >= groups.length}
                onClick={() => setGroupOffset((value) => value + 10)}
              >
                More groups
              </button>
            </div>
          )}
          <p className="muted">
            {Object.keys(selections).length}{" "}
            {Object.keys(selections).length === 1 ? "group" : "groups"} selected
            for this plan.
          </p>
          <details>
            <summary>Inspection details</summary>
            {snapshot.files.slice(0, fileLimit).map((file) => (
              <div className="import-path" key={file.path}>
                <strong>
                  {file.path} · {file.medium ? file.state : "Extra file"}
                </strong>
                {file.medium && file.reason && <span>{file.reason}</span>}
                <span className="muted">SHA-256: {file.sha256}</span>
              </div>
            ))}
            {fileLimit < snapshot.files.length && (
              <button onClick={() => setFileLimit((value) => value + 100)}>
                Show more file evidence
              </button>
            )}
          </details>
          <Notice error={settings.error || save.error} />
          <label className="check-label">
            <input
              type="checkbox"
              checked={includeCovers}
              disabled={save.isPending}
              onChange={(event) => setIncludeCovers(event.target.checked)}
            />
            Include selected catalog covers in new imports
          </label>
          {Object.values(selections).some(
            (selection) => !selection.full_content,
          ) && (
            <p className="muted">
              Confirm that each selected group contains the complete book before
              saving.
            </p>
          )}
          <button
            className="primary"
            disabled={
              save.isPending ||
              !settings.data ||
              !grouping.data ||
              editingGroups ||
              !Object.keys(selections).length ||
              Object.values(selections).some(
                (selection) =>
                  !selection.full_content ||
                  (!!selection.contained_work_ids?.length &&
                    (!selection.contents_confirmed ||
                      selection.contained_work_ids.length < 2)),
              )
            }
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save import plan"}
          </button>
          {save.isError && (
            <button
              onClick={() => {
                settings.refetch();
                grouping.refetch();
                matches.refetch();
                setSelections({});
              }}
            >
              Refresh review settings
            </button>
          )}
        </>
      )}
      <Notice error={frozen.error} />
      {frozen.data && (
        <article className="panel editor" aria-label="Saved import plan">
          <h3>Import plan saved</h3>
          <Link to={`/organization/destinations?plan=${frozen.data.id}`}>
            Check destination
          </Link>
          <p>
            {frozen.data.document.plan.expected_items} planned item folders ·{" "}
            {frozen.data.document.plan.held_items} need attention
          </p>
          <p className="notice">
            Recorded for review. Source revalidation, destination checks and
            Audiobookshelf compatibility are still required before publication.
          </p>
          <p className="muted">
            {Object.keys(frozen.data.document.cover_sources || {}).length}{" "}
            selected covers. Unavailable artwork is reported without blocking
            the book import.
          </p>
          {frozen.data.document.plan.items.map((item) => (
            <div className="import-path" key={item.group_id}>
              <strong>
                {item.title} · {item.state}
              </strong>
              {item.reason && <span>{item.reason}</span>}
              {!!frozen.data.document.collection_contents?.[item.group_id]
                ?.length && (
                <span>
                  One collection containing:{" "}
                  {frozen.data.document.collection_contents[item.group_id]
                    .map((book) => book.title)
                    .join(" · ")}
                </span>
              )}
              {(item.conversion?.sources ?? []).map((source) => (
                <span key={source}>
                  {source} →{" "}
                  {item.files?.find((file) =>
                    item.conversion?.sources?.includes(file.source),
                  )?.destination ?? item.conversion?.output_name}
                </span>
              ))}
              {(item.files || [])
                .filter(
                  (file) => !item.conversion?.sources?.includes(file.source),
                )
                .map((file) => (
                  <span key={file.source}>
                    {file.source} → {file.destination}
                  </span>
                ))}
            </div>
          ))}
          <ImportExecution plan={frozen.data} />
        </article>
      )}
    </section>
  );
}

function editionLabel(medium: string) {
  if (medium === "audio") return "Audiobook";
  if (medium === "ebook") return "Ebook";
  if (medium === "print") return "Print";
  return "Unspecified format";
}

function EditionGap({
  group,
  versions,
  disabled,
  pending,
  note,
  onCreate,
}: {
  group: Group;
  versions: {
    id: string;
    medium: string;
    title: string | null;
    publication_year: number | null;
    needs_review: boolean;
  }[];
  disabled: boolean;
  pending: boolean;
  note: string;
  onCreate: () => void;
}) {
  const format = group.medium === "audio" ? "audiobook" : "ebook";
  const sameMedium = versions.some(
    (version) => version.medium === group.medium && !version.needs_review,
  );
  const others = versions.filter((version) => version.medium !== group.medium);
  return (
    <>
      {!sameMedium && (
        <>
          <p>
            No {format} edition is listed on this page
            {others.length ? ". Other editions:" : "."}
          </p>
          {others.slice(0, 6).map((version) => (
            <div className="import-path" key={version.id}>
              {editionLabel(version.medium)} · {version.title || "Untitled"} ·{" "}
              {version.publication_year || "Year unknown"}
            </div>
          ))}
          {others.length > 6 && (
            <p className="muted">More editions are on the next page.</p>
          )}
          <button type="button" disabled={disabled} onClick={onCreate}>
            {pending
              ? "Adding edition…"
              : `Add an ${format} edition from this file`}
          </button>
          <p className="muted">
            Uses the file&apos;s title, narrators and identifiers. Saving the
            plan still asks you to confirm the files contain the complete book.
          </p>
        </>
      )}
      {note && <p role="status">{note}</p>}
    </>
  );
}

function GroupMatch({
  linkedWorkId,
  inspectionId,
  groupingRevision,
  group,
  match,
  selection,
  disabled,
  onChange,
}: {
  linkedWorkId?: string;
  inspectionId: string;
  groupingRevision: string;
  group: Group;
  match?: CatalogMatch;
  selection?: Selection;
  disabled: boolean;
  onChange: (selection: Selection | null) => void;
}) {
  const cache = useQueryClient();
  const searchTouched = useRef(false);
  const autoSearched = useRef(false);
  const [input, setInput] = useState(group.title || "");
  const [q, setQ] = useState("");
  const [editionNote, setEditionNote] = useState("");
  const [offset, setOffset] = useState(0);
  const [manualWorkId, setWorkId] = useState(linkedWorkId || "");
  const workId = selection?.work_id || manualWorkId;
  const versionId = selection?.version_id || "";
  const [versionOffset, setVersionOffset] = useState(0);
  const full = selection?.full_content || false;
  const suggested = match ? matchedSelection(match) : null;
  const selectedCandidate = match?.candidates.find(
    (item) => item.version_id === versionId,
  );
  const books = useQuery({
    queryKey: ["import-match-books", q, offset],
    enabled: !!q,
    queryFn: async () =>
      result(
        await api.GET("/api/catalog/works", {
          params: { query: { q, offset, limit: 20 } },
        }),
      ),
  });
  const versions = useQuery({
    queryKey: ["import-match-versions", workId, versionOffset],
    enabled: !!workId,
    queryFn: async () =>
      result(
        await api.GET("/api/metadata/works/{work_id}", {
          params: {
            path: { work_id: workId },
            query: { offset: versionOffset, limit: 40 },
          },
        }),
      ),
  });
  useEffect(() => {
    if (
      searchTouched.current ||
      autoSearched.current ||
      match?.status !== "unmatched" ||
      !group.title?.trim()
    )
      return;
    autoSearched.current = true;
    setQ(group.title.trim().slice(0, 300));
  }, [match, group.title]);
  function candidateBlocked(candidate: CatalogMatch["candidates"][number]) {
    return candidate.conflicts.some(
      (conflict) =>
        conflict ===
          "Resolve this catalog version's pending metadata conflict" ||
        conflict === "This catalog identity was explicitly rejected",
    );
  }
  function choose(candidate: CatalogMatch["candidates"][number]) {
    setWorkId(candidate.work_id);
    setVersionOffset(0);
    setEditionNote("");
    const automatic =
      match?.status === "matched" &&
      match.selected_version_id === candidate.version_id;
    onChange({
      group_key: group.key,
      work_id: candidate.work_id,
      version_id: candidate.version_id,
      full_content: false,
      contents_confirmed: false,
      ...(automatic && match ? { match_revision: match.revision } : {}),
    });
  }
  const createEdition = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/organization/inspections/{inspection_id}/editions",
          {
            params: { path: { inspection_id: inspectionId } },
            body: {
              work_id: workId,
              group_key: group.key,
              grouping_revision: groupingRevision,
            },
          },
        ),
      ),
    onSuccess: (edition) => {
      setWorkId(edition.work_id);
      setVersionOffset(0);
      setEditionNote(
        edition.created
          ? "Added an edition from this file. Confirm these files contain the complete book, then save the import plan."
          : "Using the catalog edition that already matches this file. Confirm these files contain the complete book, then save the import plan.",
      );
      onChange({
        group_key: group.key,
        work_id: edition.work_id,
        version_id: edition.version_id,
        full_content: false,
        contents_confirmed: false,
      });
      cache.invalidateQueries({
        queryKey: ["import-match-versions", edition.work_id],
      });
    },
  });
  function update(
    version: string,
    complete: boolean,
    matchRevision?: string | null,
  ) {
    onChange(
      version
        ? {
            group_key: group.key,
            work_id: workId,
            version_id: version,
            full_content: complete,
            contents_confirmed: false,
            match_revision: matchRevision,
            ...(version === selection?.version_id && complete
              ? {
                  contained_work_ids: selection.contained_work_ids,
                  contents_confirmed: selection.contents_confirmed,
                }
              : {}),
          }
        : null,
    );
  }
  return (
    <article
      className="panel editor"
      aria-label={`Match ${group.title || group.files[0]?.path || "book group"}`}
    >
      <h3>
        {group.title || "Unidentified book"} ·{" "}
        {group.medium === "audio" ? "Audiobook" : "Ebook"}
      </h3>
      <p className="muted">
        {group.authors.join(", ") || "Unknown author"}
        {group.narrators.length
          ? ` · ${group.narrators.join(", ")}`
          : ""} · {group.files.length} files
      </p>
      {group.same_edition && (
        <p className="muted">
          Reviewed as multiple formats of one complete edition.
        </p>
      )}
      <details>
        <summary>Original file paths</summary>
        {group.files.map((file) => (
          <div className="import-path" key={file.path}>
            {file.path}
            {file.role === "supplement" && " · Companion document"}
          </div>
        ))}
      </details>
      {match && (
        <>
          <p role="status">{match.message}</p>
          {suggested && (
            <button
              disabled={disabled}
              onClick={() => {
                const candidate = match?.candidates.find(
                  (item) => item.version_id === suggested.version_id,
                );
                if (candidate) choose(candidate);
              }}
            >
              Use matched edition
            </button>
          )}
          {!!match.candidates.length && !suggested && (
            <div className="actions">
              {match.candidates
                .filter((candidate) => !candidateBlocked(candidate))
                .slice(0, 5)
                .map((candidate) => (
                  <button
                    type="button"
                    key={candidate.version_id}
                    disabled={disabled}
                    onClick={() => choose(candidate)}
                  >
                    Use {candidate.version_title || candidate.title}
                    {candidate.authors.length
                      ? ` · ${candidate.authors.join(", ")}`
                      : ""}
                    {candidate.narrators.length
                      ? ` · ${candidate.narrators.join(", ")}`
                      : ""}
                    {candidate.year ? ` · ${candidate.year}` : ""}
                  </button>
                ))}
            </div>
          )}
          {selection?.match_revision && (
            <p className="muted">
              Selected from catalog evidence:{" "}
              {selectedCandidate?.title || "catalog edition"}. The evidence will
              be rechecked when you save.
            </p>
          )}
          <details>
            <summary>
              Match details ({match.candidates.length} editions)
            </summary>
            {(match.evidence.issues || []).map((issue) => (
              <p key={issue}>{issue}</p>
            ))}
            {match.candidates.map((candidate) => (
              <div className="import-path" key={candidate.version_id}>
                <strong>
                  {candidate.title} · {candidate.authors.join(", ")}
                </strong>
                <span>
                  {candidate.version_title || candidate.title} ·{" "}
                  {candidate.narrators.join(", ") || candidate.medium} ·{" "}
                  {candidate.year || "Year unknown"}
                </span>
                <span>
                  {candidate.reasons.join(" · ") || "No matching identifiers"}
                </span>
                {candidate.conflicts.length > 0 && (
                  <span>Review: {candidate.conflicts.join(" · ")}</span>
                )}
              </div>
            ))}
            {match.truncated && (
              <p>Additional candidates exist. Search for the book manually.</p>
            )}
          </details>
        </>
      )}
      <form
        className="inline-form"
        onSubmit={(event) => {
          event.preventDefault();
          searchTouched.current = true;
          setQ(input);
          setOffset(0);
        }}
      >
        <label>
          Find catalog book
          <input
            value={input}
            onChange={(event) => setInput(event.target.value)}
            maxLength={300}
          />
        </label>
        <button disabled={disabled || !input.trim()}>Find matching book</button>
      </form>
      <Notice error={books.error || versions.error || createEdition.error} />
      {books.data && (
        <>
          <label>
            Catalog book
            <select
              value={workId}
              disabled={disabled}
              onChange={(event) => {
                setWorkId(event.target.value);
                setVersionOffset(0);
                setEditionNote("");
                onChange(null);
              }}
            >
              <option value="">Choose a book</option>
              {books.data.items.map((book) => (
                <option key={book.id} value={book.id}>
                  {book.title} · {book.authors.join(", ")}
                </option>
              ))}
            </select>
          </label>
          {books.data.total > 20 && (
            <div className="actions">
              <button
                disabled={offset === 0}
                onClick={() => setOffset((value) => Math.max(0, value - 20))}
              >
                Previous matches
              </button>
              <button
                disabled={offset + 20 >= books.data.total}
                onClick={() => setOffset((value) => value + 20)}
              >
                More matches
              </button>
            </div>
          )}
        </>
      )}
      {versions.data && (
        <>
          <EditionGap
            group={group}
            versions={versions.data.versions}
            disabled={disabled || createEdition.isPending || !workId}
            pending={createEdition.isPending}
            note={editionNote}
            onCreate={() => createEdition.mutate()}
          />
          <label>
            Catalog version
            <select
              value={versionId}
              disabled={disabled}
              onChange={(event) => update(event.target.value, false)}
            >
              <option value="">Choose the matching edition or recording</option>
              {versionId &&
                !versions.data.versions.some(
                  (version) => version.id === versionId,
                ) && (
                  <option value={versionId}>
                    {selectedCandidate?.version_title ||
                      "Selected catalog version"}{" "}
                    · outside this page
                  </option>
                )}
              {versions.data.versions
                .filter(
                  (version) =>
                    version.medium === group.medium && !version.needs_review,
                )
                .map((version) => (
                  <option key={version.id} value={version.id}>
                    {version.title || group.title} ·{" "}
                    {version.narrators.join(", ") || version.medium} ·{" "}
                    {version.publication_year || "Year unknown"}
                    {version.owned ? " · Already in library" : ""}
                  </option>
                ))}
            </select>
          </label>
          {versions.data.versions_total > 40 && (
            <div className="actions">
              <button
                disabled={versionOffset === 0}
                onClick={() => {
                  update("", full);
                  setVersionOffset((value) => Math.max(0, value - 40));
                }}
              >
                Previous versions
              </button>
              <button
                disabled={versionOffset + 40 >= versions.data.versions_total}
                onClick={() => {
                  update("", full);
                  setVersionOffset((value) => value + 40);
                }}
              >
                More versions
              </button>
            </div>
          )}
          <label className="check-label">
            <input
              type="checkbox"
              checked={full}
              disabled={disabled || !versionId}
              onChange={(event) =>
                update(
                  versionId,
                  event.target.checked,
                  selection?.match_revision,
                )
              }
            />
            These files contain the complete book, not a sample or companion
            document
          </label>
        </>
      )}
      {selection && (
        <ImportCollectionContents
          selection={selection}
          disabled={disabled}
          onChange={onChange}
        />
      )}
    </article>
  );
}
