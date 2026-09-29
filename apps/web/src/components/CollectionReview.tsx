import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import { randomUUID } from "../randomUUID";
import BookDialog from "./BookDialog";
import BookCover from "./BookCover";
import { transferSize } from "../pages/DownloadConstraints";

type Choice = components["schemas"]["CollectionChoice"];
const titleKey = (s: string) =>
  s
    .toLowerCase()
    .replaceAll("&", "and")
    .replace(/[^\p{L}\p{N}]/gu, "");

export default function CollectionReview({
  searchId,
  resultId,
  close,
}: {
  searchId: string;
  resultId: string;
  close: () => void;
}) {
  const cache = useQueryClient();
  const [filter, setFilter] = useState("");
  const [sort, setSort] = useState("source");
  const key = useRef(randomUUID());
  const [recordings, setRecordings] = useState<Record<string, string>>({});
  const [artifactId, setArtifactId] = useState<string>();
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [catalogChoices, setCatalogChoices] = useState<Record<string, string>>(
    {},
  );
  const preview = useQuery({
    queryKey: ["collection-review", searchId, resultId, artifactId],
    queryFn: async () =>
      result(
        await api.GET(
          "/api/source-searches/{search_id}/results/{result_id}/contents",
          {
            params: {
              path: { search_id: searchId, result_id: resultId },
              query: { artifact_id: artifactId },
            },
          },
        ),
      ),
    retry: false,
    refetchOnWindowFocus: false,
  });
  const inspect = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/source-searches/{search_id}/results/{result_id}/artifact",
          { params: { path: { search_id: searchId, result_id: resultId } } },
        ),
      ),
    onSuccess: (artifact) => {
      setArtifactId(artifact.id);
      setChoices({});
    },
  });
  const download = useMutation({
    mutationFn: async (all: boolean) => {
      const data = preview.data!;
      const selected = all
        ? data.entries.flatMap((entry) => {
            if (choices[entry.id]) return [choices[entry.id]];
            const candidate =
              catalogChoices[entry.id] ||
              (entry.candidates.length === 1 ? entry.candidates[0].id : "");
            const options = entry.recording_options || [];
            const recordingId =
              recordings[entry.id] ||
              (options.length === 1 ? String(options[0].id) : undefined);
            const recording = options.find((r) => r.id === recordingId);
            const recordingPaths = recording
              ? (recording.files as string[])
              : entry.files;
            return candidate &&
              (options.length <= 1 || recordingId !== undefined) &&
              !entry.candidates.find((c) => c.id === candidate)?.owned &&
              recordingPaths.length
              ? [
                  {
                    entry_id: entry.id,
                    candidate_id: candidate,
                    recording_id: recordingId,
                    paths: recordingPaths,
                  },
                ]
              : [];
          })
        : Object.values(choices);
      return result(
        await api.POST("/api/collection-reviews/{review_id}/download", {
          params: {
            path: { review_id: data.review_id },
            header: { "idempotency-key": key.current },
          },
          body: {
            revision: data.revision,
            choices: selected,
            download_all_files: all,
          },
        }),
      );
    },
    onSuccess: async () => {
      for (const name of ["requests", "downloads", "book-sources"])
        await cache.invalidateQueries({ queryKey: [name] });
    },
  });
  const data = preview.data;
  const initialized = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!data?.artifact_id || initialized.current === data.review_id) return;
    initialized.current = data.review_id;
    const entry = data.entries.find(
      (e) =>
        e.candidates.length === 1 &&
        e.match === "exact" &&
        (e.recording_options || []).length <= 1 &&
        !e.candidates[0].owned &&
        e.files.length > 0 &&
        (titleKey(e.title) === titleKey(data.requested_title) ||
          titleKey(e.candidates[0].title) === titleKey(data.requested_title)),
    );
    if (entry)
      setChoices({
        [entry.id]: {
          entry_id: entry.id,
          candidate_id: entry.candidates[0].id,
          recording_id:
            entry.recording_options?.length === 1
              ? String(entry.recording_options[0].id)
              : undefined,
          paths: entry.files,
        },
      });
  }, [data]);
  const busy = inspect.isPending || download.isPending;
  const change = (next: Record<string, Choice>) => {
    setChoices(next);
    key.current = randomUUID();
    download.reset();
  };
  const visibleEntries = (data?.entries || []).filter((entry) =>
    entry.title.toLowerCase().includes(filter.toLowerCase()),
  );
  if (sort === "title")
    visibleEntries.sort((a, b) => a.title.localeCompare(b.title));
  if (sort === "series")
    visibleEntries.sort((a, b) => {
      const x = a.candidates[0]?.series[0];
      const y = b.candidates[0]?.series[0];
      return (
        String(x?.name || "~").localeCompare(String(y?.name || "~")) ||
        Number(x?.position || 0) - Number(y?.position || 0) ||
        a.title.localeCompare(b.title)
      );
    });
  const paths = new Set(Object.values(choices).flatMap((c) => c.paths));
  const bytes = (data?.files || [])
    .filter((f) => paths.has(f.path))
    .reduce((n, f) => n + f.size_bytes, 0);
  return (
    <BookDialog
      title="Review collection"
      close={close}
      className="release-dialog collection-dialog"
    >
      <Notice error={preview.error || inspect.error || download.error} />
      {preview.isPending ? (
        <Loading />
      ) : (
        data && (
          <>
            <p className="eyebrow">COLLECTION CONTENTS</p>
            <h2>{data.title}</h2>
            <p>
              {data.entries.length} listed titles
              {data.bibliography_count > 0 &&
                ` · ${data.bibliography_count} English catalog candidates by the author`}
            </p>
            <p className="muted">
              A collection may contain only some of an author’s books. Confirm
              the titles and files you want. Each book is checked again during
              import.
            </p>
            {(data.series_coverage || []).map((series, i) => (
              <p className="notice" key={i}>
                {String(series.included)} of {String(series.total)} observed
                main books in{" "}
                <Link to={`/series/hardcover/${series.external_id}`}>
                  {String(series.name)}
                </Link>
              </p>
            ))}
            {data.warnings.map((warning) => (
              <p className="notice" key={warning}>
                {warning}
              </p>
            ))}
            {!artifactId && (
              <div className="panel editor">
                <p>
                  Inspect the torrent to see which files can be selected. This
                  does not start a transfer or spend a Freeleech wedge.
                </p>
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() => inspect.mutate()}
                >
                  {inspect.isPending
                    ? "Reading torrent files…"
                    : "Review downloadable files"}
                </button>
              </div>
            )}
            {download.data ? (
              <p role="status" className="notice">
                {download.data.message}.{" "}
                <Link to="/requests">View requests</Link>
              </p>
            ) : (
              <>
                <div className="button-row">
                  <label>
                    Find a book
                    <input
                      value={filter}
                      onChange={(event) => setFilter(event.target.value)}
                      placeholder="Filter collection titles…"
                    />
                  </label>
                  <label>
                    Order
                    <select
                      value={sort}
                      onChange={(event) => setSort(event.target.value)}
                    >
                      <option value="source">Source order</option>
                      <option value="title">Title A–Z</option>
                      <option value="series">Series order</option>
                    </select>
                  </label>
                </div>
                <div className="collection-book-list">
                  {visibleEntries.map((entry) => {
                    const candidateId =
                      catalogChoices[entry.id] ||
                      (entry.candidates.length === 1
                        ? entry.candidates[0].id
                        : "");
                    const candidate = entry.candidates.find(
                      (c) => c.id === candidateId,
                    );
                    const choice = choices[entry.id];
                    const options = entry.recording_options || [];
                    const recordingId =
                      recordings[entry.id] ||
                      (options.length === 1 ? String(options[0].id) : "");
                    const recording = options.find((r) => r.id === recordingId);
                    const suggestedFiles = recording
                      ? (recording.files as string[])
                      : entry.files;
                    const requested =
                      titleKey(entry.title) ===
                        titleKey(data.requested_title) ||
                      (candidate &&
                        titleKey(candidate.title) ===
                          titleKey(data.requested_title));
                    return (
                      <article className="collection-book" key={entry.id}>
                        <div className="collection-cover">
                          <BookCover
                            title={entry.title}
                            cover={candidate?.cover_url}
                            actions={false}
                          />
                        </div>
                        <div className="collection-book-copy">
                          <h3>
                            {entry.title}
                            {requested && (
                              <small className="release-tag">
                                Requested book
                              </small>
                            )}
                          </h3>
                          {candidate && (
                            <p className="muted">
                              {candidate.authors.join(", ")}
                              {candidate.series.map((series, i) => (
                                <span key={i}>
                                  {" "}
                                  · {String(series.name)}
                                  {series.position
                                    ? ` #${series.position}`
                                    : ""}
                                </span>
                              ))}
                            </p>
                          )}
                          <label>
                            Catalog match
                            <select
                              aria-label={`Catalog match for ${entry.title}`}
                              value={candidateId}
                              disabled={busy}
                              onChange={(e) => {
                                setCatalogChoices({
                                  ...catalogChoices,
                                  [entry.id]: e.target.value,
                                });
                                const next = { ...choices };
                                delete next[entry.id];
                                change(next);
                              }}
                            >
                              <option value="">
                                {entry.candidates.length
                                  ? "Choose a catalog book"
                                  : "Unmatched — needs review"}
                              </option>
                              {entry.candidates.map((c) => (
                                <option key={c.id} value={c.id}>
                                  {c.title} · {c.authors.join(", ")}
                                </option>
                              ))}
                            </select>
                          </label>
                          {entry.match !== "exact" && (
                            <p className="muted">
                              {entry.match === "unmatched"
                                ? "No reliable catalog candidate found. Keep this title in review."
                                : "Confirm the catalog match; the source title is ambiguous."}
                            </p>
                          )}
                          <p className="muted">
                            {[
                              ...new Set(
                                entry.evidence.map((e) =>
                                  String(e.basis || "description"),
                                ),
                              ),
                            ].join(" · ") || "Contents need review"}
                            {" · "}
                            {entry.files.length
                              ? `${entry.files.length} filename matches`
                              : "Files not yet matched"}
                            {" · Metadata confirmation after download"}
                          </p>
                          {options.length > 0 && (
                            <label>
                              Recording
                              <select
                                aria-label={`Recording for ${entry.title}`}
                                value={recordingId}
                                disabled={busy}
                                onChange={(e) => {
                                  setRecordings({
                                    ...recordings,
                                    [entry.id]: e.target.value,
                                  });
                                  const next = { ...choices };
                                  delete next[entry.id];
                                  change(next);
                                }}
                              >
                                <option value="">Choose a recording</option>
                                {options.map((r) => (
                                  <option
                                    key={String(r.id)}
                                    value={String(r.id)}
                                  >
                                    {Object.values(
                                      r.claims as Record<string, unknown>,
                                    ).join(" · ")}
                                  </option>
                                ))}
                              </select>
                            </label>
                          )}
                          {entry.recordings.length > 0 && (
                            <details>
                              <summary>Recording and source evidence</summary>
                              <ul>
                                {entry.recordings.map((r, i) => (
                                  <li key={i}>
                                    {Object.values(r).join(" · ")}
                                  </li>
                                ))}
                              </ul>
                            </details>
                          )}
                          {artifactId && (
                            <>
                              <label className="check-label">
                                <input
                                  type="checkbox"
                                  checked={!!choice}
                                  disabled={
                                    busy ||
                                    !candidateId ||
                                    candidate?.owned ||
                                    (options.length > 1 && !recordingId)
                                  }
                                  onChange={(e) => {
                                    const next = { ...choices };
                                    if (e.target.checked)
                                      next[entry.id] = {
                                        entry_id: entry.id,
                                        candidate_id: candidateId,
                                        recording_id: recordingId || undefined,
                                        paths: suggestedFiles,
                                      };
                                    else delete next[entry.id];
                                    change(next);
                                  }}
                                />
                                {candidate?.owned
                                  ? "Already in your library"
                                  : "Select this book"}
                                {suggestedFiles.length
                                  ? ` · ${suggestedFiles.length} suggested files`
                                  : " · choose files below"}
                              </label>
                              {choice && (
                                <details open={!suggestedFiles.length}>
                                  <summary>
                                    Review files ({choice.paths.length})
                                  </summary>
                                  <div className="collection-files">
                                    {data.files.map((file) => (
                                      <label
                                        className="check-label"
                                        key={file.path}
                                      >
                                        <input
                                          type="checkbox"
                                          checked={choice.paths.includes(
                                            file.path,
                                          )}
                                          disabled={
                                            busy ||
                                            (!choice.paths.includes(
                                              file.path,
                                            ) &&
                                              paths.has(file.path))
                                          }
                                          onChange={(e) =>
                                            change({
                                              ...choices,
                                              [entry.id]: {
                                                ...choice,
                                                paths: e.target.checked
                                                  ? [...choice.paths, file.path]
                                                  : choice.paths.filter(
                                                      (p) => p !== file.path,
                                                    ),
                                              },
                                            })
                                          }
                                        />
                                        <span>
                                          {file.path}
                                          <small className="block muted">
                                            {transferSize(file.size_bytes)}
                                          </small>
                                        </span>
                                      </label>
                                    ))}
                                  </div>
                                </details>
                              )}
                            </>
                          )}
                        </div>
                      </article>
                    );
                  })}
                </div>
                {data.excluded.length > 0 && (
                  <p className="muted">
                    {data.excluded.length} explicitly excluded titles were kept
                    out of this collection.
                  </p>
                )}
                {artifactId && (
                  <div className="collection-actions">
                    <Link to={`/sources/artifacts/${artifactId}`}>
                      Open full release review
                    </Link>
                    <p>
                      {Object.keys(choices).length} books selected ·{" "}
                      {paths.size} files · {transferSize(bytes)}
                    </p>
                    <p className="muted">
                      Unselected files are skipped. Download all also includes
                      unmatched files; those need review before import. Choose
                      one recording per book. Other downloaded versions remain
                      in import review.
                    </p>
                    <div className="button-row">
                      <button
                        className="primary"
                        disabled={
                          busy ||
                          !Object.keys(choices).length ||
                          Object.values(choices).some((c) => !c.paths.length)
                        }
                        onClick={() => download.mutate(false)}
                      >
                        {download.isPending
                          ? "Preparing collection…"
                          : "Download selected books"}
                      </button>
                      <button
                        disabled={
                          busy ||
                          Object.values(choices).some((c) => !c.paths.length) ||
                          !(
                            Object.keys(choices).length ||
                            data.entries.some(
                              (e) =>
                                e.candidates.length === 1 &&
                                !e.candidates[0].owned &&
                                e.files.length > 0 &&
                                ((e.recording_options || []).length <= 1 ||
                                  (e.recording_options || []).some(
                                    (r) =>
                                      r.id === recordings[e.id] &&
                                      (r.files as string[]).length > 0,
                                  )),
                            )
                          )
                        }
                        onClick={() => {
                          key.current = randomUUID();
                          download.mutate(true);
                        }}
                      >
                        Download all files (
                        {transferSize(
                          data.files.reduce((n, f) => n + f.size_bytes, 0),
                        )}
                        )
                      </button>
                    </div>
                  </div>
                )}
              </>
            )}
          </>
        )
      )}
    </BookDialog>
  );
}
