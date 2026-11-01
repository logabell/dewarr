import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import BookDialog from "./BookDialog";
import {
  releaseDate,
  releaseIsNewer,
  releaseNoteBlocks,
  versionLabel,
  type Inline,
  type NoteBlock,
} from "./releaseNotes";

type Release = components["schemas"]["ApplicationRelease"];
type ReleaseNote = components["schemas"]["ApplicationReleaseNote"];

function renderInlines(items: Inline[]) {
  return items.map((item, index) => {
    if (item.type === "text") return item.value;
    if (item.type === "code") return <code key={index}>{item.value}</code>;
    if (item.type === "strong")
      return <strong key={index}>{item.value}</strong>;
    return (
      <a key={index} href={item.href} target="_blank" rel="noreferrer">
        {item.text}
      </a>
    );
  });
}

function NoteBlockView({ block }: { block: NoteBlock }) {
  if (block.type === "heading") {
    const Tag = block.level === 1 ? "h3" : block.level === 2 ? "h4" : "h5";
    return <Tag>{renderInlines(block.inlines)}</Tag>;
  }
  if (block.type === "list") {
    const List = block.ordered ? "ol" : "ul";
    return (
      <List>
        {block.items.map((item, index) => (
          <li key={index}>{renderInlines(item)}</li>
        ))}
      </List>
    );
  }
  if (block.type === "code")
    return (
      <pre>
        <code>{block.value}</code>
      </pre>
    );
  return <p>{renderInlines(block.inlines)}</p>;
}

function headingText(block: NoteBlock) {
  return block.type === "heading"
    ? block.inlines
        .map((item) => (item.type === "link" ? item.text : item.value))
        .join("")
        .trim()
    : "";
}

function ReleaseNotes({ source, title }: { source: string; title: string }) {
  const all = releaseNoteBlocks(source);
  // Notes often start with the release's own name, which the entry already shows as its title.
  const blocks =
    all[0] && headingText(all[0]) === title.trim() ? all.slice(1) : all;
  if (!blocks.length)
    return <p className="muted">No notes for this release.</p>;
  return (
    <div className="release-notes">
      {blocks.map((block, index) => (
        <NoteBlockView key={index} block={block} />
      ))}
    </div>
  );
}

function ReleaseEntry({
  release,
  installed,
  primary = false,
}: {
  release: ReleaseNote;
  installed: string;
  primary?: boolean;
}) {
  const published = releaseDate(release.published_at);
  const Title = primary ? "h2" : "h3";
  return (
    <article className="release-note">
      <Title>{release.name}</Title>
      <p className="release-note-meta">
        <span>{versionLabel(release.version)}</span>
        {published && <span>{published}</span>}
        {release.prerelease && <span>Pre-release</span>}
        {releaseIsNewer(installed, release.version) && (
          <span className="available-tag">Available</span>
        )}
      </p>
      <ReleaseNotes source={release.notes} title={release.name} />
      <a href={release.url} target="_blank" rel="noreferrer">
        View on GitHub
      </a>
    </article>
  );
}

function historyMessage(
  status: string,
  installed: string,
  latest: string | null,
  updateAvailable: boolean,
) {
  if (status === "unconfigured")
    return "Release tracking is not configured, so notes from GitHub are unavailable.";
  if (status === "no-release")
    return "No GitHub releases have been published yet.";
  if (status === "unavailable")
    return "Release notes could not be loaded from GitHub.";
  if (updateAvailable && latest) return `${versionLabel(latest)} is available.`;
  if (!/^v?\d+\.\d+\.\d+(?:\+[\w.-]+)?$/.test(installed))
    return "This build is not a published version. The notes below are from GitHub.";
  return "This installation is up to date.";
}

function ReleaseHistory({
  installed,
  latest,
  status,
  updateAvailable,
  close,
}: {
  installed: string;
  latest: string | null;
  status: string;
  updateAvailable: boolean;
  close: () => void;
}) {
  const notes = useQuery({
    queryKey: ["application-releases"],
    queryFn: async () => result(await api.GET("/api/application/releases")),
    enabled: status !== "unconfigured",
    staleTime: 60 * 60 * 1000,
    retry: false,
  });
  const releases = notes.data?.status === "checked" ? notes.data.releases : [];
  const featured =
    releases.find((release) => !release.prerelease) ?? releases[0];
  const earlier = featured
    ? releases.filter((release) => release !== featured)
    : [];
  const message = historyMessage(
    notes.isError ? "unavailable" : notes.data?.status || status,
    installed,
    latest,
    updateAvailable,
  );
  return (
    <BookDialog
      title="Release notes"
      close={close}
      className="application-release-dialog"
    >
      <div className="application-release-history">
        <p className="muted">Installed {versionLabel(installed)}</p>
        {notes.isLoading ? (
          <p>Loading release notes…</p>
        ) : (
          <>
            {message && <p>{message}</p>}
            {featured && (
              <ReleaseEntry release={featured} installed={installed} primary />
            )}
            {earlier.length > 0 && (
              <section className="earlier-releases">
                <h2>Earlier releases</h2>
                {earlier.map((release) => (
                  <ReleaseEntry
                    key={release.version}
                    release={release}
                    installed={installed}
                  />
                ))}
              </section>
            )}
          </>
        )}
      </div>
    </BookDialog>
  );
}

export function ApplicationRelease() {
  const [open, setOpen] = useState(false);
  const { data, isError } = useQuery({
    queryKey: ["application-release"],
    queryFn: async () => result(await api.GET("/api/application/release")),
    staleTime: 60 * 60 * 1000,
    refetchInterval: 60 * 60 * 1000,
    retry: false,
  });
  if (!data?.installed_version)
    return (
      <footer className="application-release">
        <small>{isError ? "Version unavailable" : "Checking version…"}</small>
      </footer>
    );
  const label = versionLabel(data.installed_version);
  return (
    <footer
      className="application-release"
      role="group"
      aria-label="Application version"
    >
      <button
        type="button"
        className="installed-version text-button"
        aria-haspopup="dialog"
        title="Show release notes"
        onClick={() => setOpen(true)}
      >
        {label}
      </button>
      {data.update_available ? (
        <button
          type="button"
          className="release-update text-button"
          aria-haspopup="dialog"
          title={`Version ${data.latest_version} is available`}
          onClick={() => setOpen(true)}
        >
          Update
        </button>
      ) : (
        <small>{footerStatus(data)}</small>
      )}
      {open && (
        <ReleaseHistory
          installed={data.installed_version}
          latest={data.latest_version}
          status={data.status}
          updateAvailable={data.update_available}
          close={() => setOpen(false)}
        />
      )}
    </footer>
  );
}

function footerStatus(data: Release) {
  if (data.status === "checked")
    return /^v?\d+\.\d+\.\d+(?:\+[\w.-]+)?$/.test(data.installed_version)
      ? "Up to date"
      : "Unreleased build";
  if (data.status === "unconfigured") return "Release tracking not configured";
  if (data.status === "no-release") return "No releases yet";
  return "Update check unavailable";
}
