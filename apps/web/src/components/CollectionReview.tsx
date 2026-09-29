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
type Entry = components["schemas"]["PackContentsEntry"];
const candidateDefault = (entry: Entry) =>
  entry.suggested_candidate_id ||
  (entry.match === "exact" && entry.candidates.length === 1
    ? entry.candidates[0].id
    : "");
const recordingDefault = (entry: Entry) =>
  entry.suggested_recording_id ||
  (entry.recording_options?.length === 1
    ? String(entry.recording_options[0].id)
    : "");
const filesFor = (entry: Entry, recording: string) => {
  const option = entry.recording_options?.find((r) => r.id === recording);
  return option ? (option.files as string[]) : entry.files;
};

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
  const [expanded, setExpanded] = useState<string>();
  const [matchSearch, setMatchSearch] = useState("");
  const key = useRef(randomUUID());
  const [recordings, setRecordings] = useState<Record<string, string>>({});
  const [artifactId, setArtifactId] = useState<string>();
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [catalogChoices, setCatalogChoices] = useState<Record<string, string>>(
    {},
  );
  const inspect = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/source-searches/{search_id}/results/{result_id}/artifact",
          { params: { path: { search_id: searchId, result_id: resultId } } },
        ),
      ),
    onSuccess: (artifact) => setArtifactId(artifact.id),
  });
  const started = useRef(false);
  useEffect(() => {
    if (!started.current) {
      started.current = true;
      inspect.mutate();
    }
  }, [inspect.mutate]);
  const preview = useQuery({
    queryKey: ["collection-review", searchId, resultId, artifactId],
    enabled: !!artifactId,
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
  const download = useMutation({
    mutationFn: async (all: boolean) =>
      result(
        await api.POST("/api/collection-reviews/{review_id}/download", {
          params: {
            path: { review_id: preview.data!.review_id },
            header: { "idempotency-key": key.current },
          },
          body: {
            revision: preview.data!.revision,
            choices: Object.values(choices),
            download_all_files: all,
          },
        }),
      ),
    onSuccess: async () => {
      await Promise.all(
        ["requests", "downloads", "book-sources"].map((name) =>
          cache.invalidateQueries({ queryKey: [name] }),
        ),
      );
    },
  });
  const data = preview.data;
  const initialized = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (!data?.artifact_id || initialized.current === data.review_id) return;
    initialized.current = data.review_id;
    const selected: Record<string, Choice> = {};
    const paths = new Set<string>();
    const books = new Set<string>();
    for (const entry of data.entries) {
      const candidateId = candidateDefault(entry);
      const candidate = entry.candidates.find((c) => c.id === candidateId);
      const recordingId = recordingDefault(entry);
      const files = filesFor(entry, recordingId);
      const bookId = candidate?.work_id || candidateId;
      if (
        !candidate ||
        candidate.owned ||
        !files.length ||
        ((entry.recording_options?.length || 0) > 1 && !recordingId) ||
        files.some((path) => paths.has(path)) ||
        books.has(bookId)
      )
        continue;
      selected[entry.id] = {
        entry_id: entry.id,
        candidate_id: candidateId,
        recording_id: recordingId || undefined,
        paths: files,
      };
      files.forEach((path) => paths.add(path));
      books.add(bookId);
    }
    setChoices(selected);
  }, [data]);
  const busy = download.isPending;
  const change = (next: Record<string, Choice>) => {
    setChoices(next);
    key.current = randomUUID();
    download.reset();
  };
  const allCandidates = data?.catalog_candidates?.length
    ? data.catalog_candidates
    : data?.entries.flatMap((e) => e.candidates) || [];
  const candidateFor = (entry: Entry) =>
    allCandidates.find(
      (c) => c.id === (catalogChoices[entry.id] ?? candidateDefault(entry)),
    );
  const ready = (entry: Entry) => {
    const recording = recordings[entry.id] ?? recordingDefault(entry);
    return (
      !!candidateFor(entry) &&
      (choices[entry.id]?.paths.length || filesFor(entry, recording).length) >
        0 &&
      ((entry.recording_options?.length || 0) <= 1 || !!recording)
    );
  };
  const sorted = [...(data?.entries || [])].sort((a, b) => {
    const x = candidateFor(a)?.series[0];
    const y = candidateFor(b)?.series[0];
    return (
      String(x?.name || "~").localeCompare(String(y?.name || "~")) ||
      Number(x?.position || 0) - Number(y?.position || 0) ||
      a.title.localeCompare(b.title)
    );
  });
  const visible = (entry: Entry) =>
    `${entry.title} ${candidateFor(entry)?.title || ""}`
      .toLowerCase()
      .includes(filter.toLowerCase());
  const matched = sorted.filter((e) => ready(e) && !candidateFor(e)?.owned);
  const owned = sorted.filter((e) => candidateFor(e)?.owned);
  const unresolved = sorted.filter((e) => !ready(e) && !candidateFor(e)?.owned);
  const paths = new Set(Object.values(choices).flatMap((c) => c.paths));
  const bytes = (data?.files || [])
    .filter((f) => paths.has(f.path))
    .reduce((n, f) => n + f.size_bytes, 0);
  const bookKey = (id: string) =>
    allCandidates.find((c) => c.id === id)?.work_id || id;
  const chosenElsewhere = (entry: Entry, id: string) =>
    Object.values(choices).some(
      (c) => c.entry_id !== entry.id && bookKey(c.candidate_id) === bookKey(id),
    );
  const select = (
    entry: Entry,
    selected: boolean,
    candidateId?: string,
    recordingId?: string,
  ) => {
    const next = { ...choices };
    const candidate = candidateId ?? candidateFor(entry)?.id;
    const recording =
      recordingId ?? recordings[entry.id] ?? recordingDefault(entry);
    const files = filesFor(entry, recording);
    const sharedFiles = Object.values(choices).some(
      (c) =>
        c.entry_id !== entry.id && c.paths.some((path) => files.includes(path)),
    );
    if (
      selected &&
      candidate &&
      !chosenElsewhere(entry, candidate) &&
      !sharedFiles
    )
      next[entry.id] = {
        entry_id: entry.id,
        candidate_id: candidate,
        recording_id: recording || undefined,
        paths: filesFor(entry, recording),
      };
    else delete next[entry.id];
    change(next);
  };
  const renderEntry = (entry: Entry) => {
    const candidate = candidateFor(entry);
    const choice = choices[entry.id];
    const recordingId = recordings[entry.id] ?? recordingDefault(entry);
    const options = entry.recording_options || [];
    const files = filesFor(entry, recordingId);
    const conflict =
      files.some((path) => paths.has(path) && !choice?.paths.includes(path)) ||
      (!!candidate && chosenElsewhere(entry, candidate.id));
    return (
      <article className="collection-book" key={entry.id}>
        <input
          type="checkbox"
          aria-label={`Select ${candidate?.title || entry.title}`}
          checked={!!choice}
          disabled={busy || !ready(entry) || candidate?.owned || conflict}
          onChange={(e) => select(entry, e.target.checked)}
        />
        <div className="collection-cover">
          <BookCover
            title={candidate?.title || entry.title}
            cover={candidate?.cover_url}
            actions={false}
          />
        </div>
        <div className="collection-book-copy">
          <h3>{candidate?.title || entry.title}</h3>
          <p className="muted">
            {candidate?.authors.join(", ") || "Match needed"}
            {candidate?.series[0]?.position
              ? ` · ${candidate.series[0].name} #${candidate.series[0].position}`
              : ""}
          </p>
          <p className="muted collection-book-status">
            {candidate?.owned
              ? "Already in your library"
              : ready(entry)
                ? `${files.length} ${files.length === 1 ? "file" : "files"} matched`
                : !candidate
                  ? "Choose a book match"
                  : options.length > 1 && !recordingId
                    ? "Choose a recording"
                    : "Files need review"}
          </p>
          <button
            className="collection-edit"
            disabled={busy}
            aria-expanded={expanded === entry.id}
            onClick={() => {
              setExpanded(expanded === entry.id ? undefined : entry.id);
              setMatchSearch("");
            }}
          >
            Change match or files
          </button>
          {expanded === entry.id && (
            <div className="collection-match-editor">
              <label>
                Search library / author catalog
                <input
                  aria-label={`Search matches for ${entry.title}`}
                  value={matchSearch}
                  onChange={(e) => setMatchSearch(e.target.value)}
                  placeholder="Book title or author…"
                />
              </label>
              <div className="collection-match-results">
                {allCandidates
                  .filter(
                    (c, i, all) =>
                      all.findIndex((b) => b.id === c.id) === i &&
                      `${c.title} ${c.authors.join(" ")}`
                        .toLowerCase()
                        .includes(matchSearch.toLowerCase()),
                  )
                  .slice(0, 40)
                  .map((c) => (
                    <button
                      key={c.id}
                      aria-pressed={candidate?.id === c.id}
                      disabled={busy || c.owned || chosenElsewhere(entry, c.id)}
                      onClick={() => {
                        setCatalogChoices({
                          ...catalogChoices,
                          [entry.id]: c.id,
                        });
                        select(
                          entry,
                          !!files.length &&
                            (options.length <= 1 || !!recordingId),
                          c.id,
                        );
                      }}
                    >
                      {c.title}
                      <span className="muted">
                        {" "}
                        · {c.authors.join(", ")}
                        {c.owned ? " · In library" : ""}
                      </span>
                    </button>
                  ))}
              </div>
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
                      select(
                        entry,
                        !!candidate &&
                          !!filesFor(entry, e.target.value).length &&
                          !!e.target.value,
                        candidate?.id,
                        e.target.value,
                      );
                    }}
                  >
                    <option value="">Choose a recording</option>
                    {options.map((r) => (
                      <option key={String(r.id)} value={String(r.id)}>
                        {Object.entries(r.claims as Record<string, unknown>)
                          .filter(([key]) => key !== "alternate")
                          .map(([, value]) => String(value))
                          .join(" · ")}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <details>
                <summary>Source evidence</summary>
                <p className="muted">
                  {entry.evidence
                    .map((e) => String(e.raw || e.basis || "Description"))
                    .join(" · ")}
                </p>
              </details>
              <details open={!files.length}>
                <summary>
                  Review files ({choice?.paths.length || files.length})
                </summary>
                <div className="collection-files">
                  {data!.files.map((file) => (
                    <label className="check-label" key={file.path}>
                      <input
                        type="checkbox"
                        checked={choice?.paths.includes(file.path) || false}
                        disabled={
                          busy ||
                          !candidate ||
                          candidate.owned ||
                          (options.length > 1 && !recordingId) ||
                          (!choice?.paths.includes(file.path) &&
                            paths.has(file.path))
                        }
                        onChange={(e) => {
                          const selected = e.target.checked
                            ? [...(choice?.paths || []), file.path]
                            : (choice?.paths || []).filter(
                                (p) => p !== file.path,
                              );
                          const next = { ...choices };
                          if (selected.length && candidate)
                            next[entry.id] = {
                              entry_id: entry.id,
                              candidate_id: candidate.id,
                              recording_id: recordingId || undefined,
                              paths: selected,
                            };
                          else delete next[entry.id];
                          change(next);
                        }}
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
            </div>
          )}
        </div>
      </article>
    );
  };
  return (
    <BookDialog
      title="Review collection"
      close={close}
      className="release-dialog collection-dialog"
    >
      <Notice error={preview.error || inspect.error || download.error} />
      {!data ? (
        <div className="collection-loading">
          {inspect.isError || preview.isError ? (
            <button
              onClick={() =>
                artifactId ? preview.refetch() : inspect.mutate()
              }
            >
              Try matching again
            </button>
          ) : (
            <>
              <Loading />
              <p>Matching books to the torrent files…</p>
              <p className="muted">Your download starts after you confirm.</p>
            </>
          )}
        </div>
      ) : (
        <>
          <div className="collection-heading">
            <p className="eyebrow">ADD TO YOUR LIBRARY</p>
            <h2>{data.title}</h2>
            <p className="muted">
              {matched.length} books matched to files. Uncheck any you don’t
              want.
            </p>
          </div>
          {download.data ? (
            <p role="status" className="notice">
              {download.data.message}. <Link to="/requests">View requests</Link>
            </p>
          ) : (
            <>
              <div className="collection-toolbar">
                <input
                  aria-label="Filter collection books"
                  placeholder="Find a book in this collection…"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                />
                <button disabled={busy} onClick={() => change({})}>
                  Clear selection
                </button>
              </div>
              <div className="collection-book-list">
                {matched.filter(visible).map(renderEntry)}
                {!matched.length && (
                  <p>
                    No books could be matched automatically. Review the items
                    below to choose their titles and files.
                  </p>
                )}
              </div>
              {unresolved.length > 0 && (
                <details className="collection-unresolved">
                  <summary>
                    {unresolved.length} items need a match or recording choice
                  </summary>
                  <p className="muted">
                    These items are not included until their books and files are
                    selected.
                  </p>
                  {unresolved.filter(visible).map(renderEntry)}
                </details>
              )}
              {owned.length > 0 && (
                <details className="collection-unresolved">
                  <summary>{owned.length} already in your library</summary>
                  {owned.filter(visible).map(renderEntry)}
                </details>
              )}
              <details className="collection-extra">
                <summary>Collection details and other files</summary>
                {data.series_coverage?.map((s, i) => (
                  <p key={i}>
                    {String(s.included)} of {String(s.total)} main books in{" "}
                    <Link to={`/series/hardcover/${s.external_id}`}>
                      {String(s.name)}
                    </Link>
                  </p>
                ))}
                {data.warnings.map((w) => (
                  <p key={w}>{w}</p>
                ))}
                <p className="muted">
                  Each book’s metadata is checked again during import. Unmatched
                  files and alternate recordings remain in review.
                </p>
                <Link to={`/sources/artifacts/${artifactId}`}>
                  Open full release review
                </Link>
                <p>
                  <button
                    disabled={
                      busy ||
                      !Object.keys(choices).length ||
                      Object.values(choices).some((c) => !c.paths.length)
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
                </p>
              </details>
              <div className="collection-actions">
                <div>
                  <strong>
                    {Object.keys(choices).length}{" "}
                    {Object.keys(choices).length === 1 ? "book" : "books"}{" "}
                    selected
                  </strong>
                  <span className="block muted">
                    {paths.size} {paths.size === 1 ? "file" : "files"} ·{" "}
                    {transferSize(bytes)}
                  </span>
                </div>
                <button
                  className="primary"
                  disabled={
                    busy ||
                    !Object.keys(choices).length ||
                    Object.values(choices).some((c) => !c.paths.length)
                  }
                  onClick={() => download.mutate(false)}
                >
                  {busy ? "Preparing collection…" : "Download selected books"}
                </button>
              </div>
            </>
          )}
        </>
      )}
    </BookDialog>
  );
}
