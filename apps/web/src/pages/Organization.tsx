import { useEffect, useId, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import { BookOpen, Headphones, Folder, FileText } from "lucide-react";
import Sortable from "../components/Sortable";
import { Link } from "react-router-dom";
import {
  useLibraryFolderSettings,
  selectLibraryDestination,
} from "./libraryFolderSettings";
import SettingHelp from "../components/SettingHelp";
import {
  folderTemplate,
  templateSegments,
  toggleSegment,
  readChoices,
  simpleChoices,
  seriesChoices,
  namingExamples,
  seriesIndexFilename,
  seriesIndexFolder,
  illustrate,
  filenameStyles,
  parseSegment,
  withJoin,
  type Medium,
  type NamingChoices,
  type TokenJoin,
} from "./namingBuilder";

type Profile = components["schemas"]["NamingProfile"];
type Settings = components["schemas"]["SettingsView"];
export default function Organization({
  embedded = false,
}: {
  embedded?: boolean;
}) {
  const [medium, setMedium] = useState<Medium>("ebook");
  const query = useQuery({
    queryKey: ["organization"],
    refetchOnWindowFocus: false,
    queryFn: async () => {
      const [settings, defaults] = await Promise.all([
        api.GET("/api/organization/settings").then(result),
        api.GET("/api/organization/defaults").then(result),
      ]);
      return { settings, defaults };
    },
  });
  if (query.isPending) return <Loading />;
  if (!query.data) return <Notice error={query.error} />;
  return (
    <Editor
      embedded={embedded}
      medium={medium}
      setMedium={setMedium}
      key={query.data.settings.revision}
      settings={query.data.settings}
      defaults={query.data.defaults}
    />
  );
}

function Editor({
  embedded,
  settings,
  defaults,
  medium,
  setMedium,
}: {
  embedded: boolean;
  settings: Settings;
  defaults: Profile;
  medium: Medium;
  setMedium: (medium: Medium) => void;
}) {
  const cache = useQueryClient();
  const folders = useLibraryFolderSettings();
  const [draft, setDraft] = useState(settings.profile);
  const [previewProfile, setPreviewProfile] = useState(draft);
  const [showJoins, setShowJoins] = useState(false);
  useEffect(() => {
    const timer = window.setTimeout(() => setPreviewProfile(draft), 250);
    return () => window.clearTimeout(timer);
  }, [draft]);
  const namingDestinations = Object.fromEntries(
    (["ebook", "audio"] as const).flatMap((kind) => {
      const selected = selectLibraryDestination(
        folders.data?.destinations || [],
        folders.data?.defaults.effective?.[`${kind}_destination_id`],
        kind,
      );
      return selected?.enabled ? [[kind, selected.id]] : [];
    }),
  );
  const preview = useQuery({
    queryKey: [
      "organization-preview",
      "builder",
      previewProfile,
      namingDestinations,
      folders.data?.destinations.map((row) => row.shared_root),
    ],
    retry: false,
    placeholderData: (previous) => previous,
    queryFn: async () =>
      result(
        await api.POST("/api/organization/preview", {
          body: {
            profile: previewProfile,
            groups: namingExamples,
            destinations: namingDestinations,
          },
        }),
      ),
  });
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/organization/settings", {
          body: { profile: draft, expected_revision: settings.revision },
        }),
      ),
    onSuccess: (settings) =>
      cache.setQueryData(["organization"], { settings, defaults }),
  });
  const folder = `${medium}_folder` as const;
  const filename = `${medium}_filename` as const;
  const choices = readChoices(draft[folder], medium);
  const changed = JSON.stringify(draft) !== JSON.stringify(settings.profile);
  const current =
    JSON.stringify(previewProfile) === JSON.stringify(draft) &&
    !preview.isFetching;
  const example = preview.data?.items.find((item) => item.medium === medium);
  const path = example?.files?.[0]?.destination;
  const destination = selectLibraryDestination(
    folders.data?.destinations || [],
    folders.data?.defaults.effective?.[`${medium}_destination_id`],
    medium,
  );
  // The planner returns an illustrative media root; substitute only that root.
  const relativePath = path?.split("/").slice(1).join("/");
  const fullPath = relativePath
    ? `${destination?.backend_path?.replace(/\/$/, "") || (medium === "audio" ? "audiobooks" : "ebooks")}/${relativePath}`
    : undefined;
  const options: [keyof NamingChoices, string][] = [
    ["author", "Author"],
    ["series", "Series"],
    ["sequence", "Sequence"],
    ["year", medium === "audio" ? "Recording year" : "Edition year"],
    ["version", medium === "audio" ? "Narrator" : "Edition"],
    ["language", "Language"],
    ["publisher", "Publisher"],
  ];
  const extension = medium === "audio" ? "mp3" : "epub";
  const folderExample = (template: string) =>
    illustrate(template, medium).replaceAll("/", " / ");
  const fileExample = (template: string) =>
    `${illustrate(template, medium)}.${extension}`;
  const styles = filenameStyles(medium);
  const layouts = [
    { name: "Recommended", template: defaults[folder] },
    { name: "By author", template: folderTemplate(medium, simpleChoices) },
    { name: "By series", template: folderTemplate(medium, seriesChoices) },
    { name: "Sequence title", template: seriesIndexFolder },
  ];
  const recommendedOn =
    draft.layout === defaults.layout && draft[folder] === defaults[folder];
  const pathParts = fullPath?.split("/") ?? [];
  const previewFile = pathParts.at(-1);
  const previewFolder = pathParts.slice(0, -1).join("/");
  const libraryRoot =
    destination?.backend_path?.replace(/\/$/, "") ||
    (medium === "audio" ? "audiobooks" : "ebooks");
  const previewFolderLabel = previewFolder.startsWith(libraryRoot)
    ? [
        libraryRoot,
        ...previewFolder.slice(libraryRoot.length).split("/").filter(Boolean),
      ].join(" / ")
    : previewFolder;
  const filenameSegments = templateSegments(draft[filename]);
  return (
    <section className="naming-builder" aria-label="Organization settings">
      {!embedded && <h1>File naming</h1>}
      <div className="segmented-control" role="group" aria-label="Book format">
        <button
          type="button"
          aria-pressed={medium === "ebook"}
          onClick={() => setMedium("ebook")}
        >
          <BookOpen size={16} />
          Ebook
        </button>
        <button
          type="button"
          aria-pressed={medium === "audio"}
          onClick={() => setMedium("audio")}
        >
          <Headphones size={16} />
          Audiobook
        </button>
      </div>
      <div className="naming-workspace">
        <div className="naming-options">
          <div
            className="folder-preview"
            aria-label="Folder preview"
            aria-busy={!current}
          >
            <div className="folder-preview-heading">
              <span className="setting-subheading">
                Preview{" "}
                <SettingHelp label="naming preview">
                  A sample book, so the folder and the file can be read
                  separately. Imports use the selected library folder and the
                  metadata available for that book.
                </SettingHelp>
              </span>
              <span>{medium === "audio" ? "Audiobook" : "Ebook"}</span>
            </div>
            {fullPath && previewFile && (
              <div
                className="naming-result"
                aria-label="Example destination path"
              >
                <div className="naming-result-line">
                  <span>Folder</span>
                  <code>{previewFolderLabel}</code>
                </div>
                <div className="naming-result-line">
                  <span>File</span>
                  <code>{previewFile}</code>
                </div>
              </div>
            )}
            {!path && (
              <p>
                {preview.isFetching
                  ? "Building preview…"
                  : example?.reason || "Preview unavailable"}
              </p>
            )}
            {!current && path && (
              <span className="preview-updating">Updating…</span>
            )}
            {example?.warnings?.map((warning) => (
              <p className="notice" key={warning}>
                {warning}
              </p>
            ))}
            {path && (
              <section className="naming-tree-section" aria-label="Folder tree">
                <ol className="folder-tree">
                  <li>
                    <Folder size={14} />
                    <code>
                      {destination?.backend_path ||
                        (medium === "audio" ? "audiobooks" : "ebooks")}
                    </code>
                    <Link to="/settings#libraries">Change folder</Link>
                  </li>
                  {relativePath?.split("/").map((part, index, parts) => (
                    <li
                      key={index}
                      style={{ paddingInlineStart: `${(index + 1) * 12}px` }}
                    >
                      {index === parts.length - 1 ? (
                        <FileText size={14} />
                      ) : (
                        <Folder size={14} />
                      )}
                      <span>{part}</span>
                    </li>
                  ))}
                </ol>
              </section>
            )}
          </div>
          <fieldset className="naming-block">
            <legend>Folder layout</legend>
            <p className="naming-section-note">These folders group the book.</p>
            <div className="naming-choice-grid">
              {layouts.map((layout) => (
                <ChoiceButton
                  key={layout.name}
                  name={layout.name}
                  example={folderExample(layout.template)}
                  pressed={
                    layout.name === "Recommended"
                      ? recommendedOn
                      : !recommendedOn &&
                        draft.layout === "conventional" &&
                        draft[folder] === layout.template
                  }
                  disabled={save.isPending}
                  onClick={() => {
                    setShowJoins(false);
                    setDraft({
                      ...draft,
                      layout:
                        layout.name === "Recommended"
                          ? defaults.layout
                          : "conventional",
                      [folder]: layout.template,
                    });
                  }}
                />
              ))}
            </div>
            {draft[folder] === seriesIndexFolder &&
              draft[filename] !== seriesIndexFilename && (
                <div className="naming-pair-hint">
                  <p>
                    The number shares the title’s folder. The matching file name
                    is “{fileExample(seriesIndexFilename)}”.
                  </p>
                  <button
                    type="button"
                    className="naming-text-button"
                    disabled={save.isPending}
                    onClick={() =>
                      setDraft({
                        ...draft,
                        rename_files: true,
                        [filename]: seriesIndexFilename,
                      })
                    }
                  >
                    Use number, series, and year
                  </button>
                </div>
              )}
            {choices && draft.layout === "conventional" ? (
              <div>
                <h3>Fields in the folder</h3>
                <div className="metadata-toggles">
                  <label className="check-label">
                    <input type="checkbox" checked disabled />
                    Title
                  </label>
                  {options.map(([key, label]) => (
                    <label className="check-label" key={key}>
                      <input
                        type="checkbox"
                        checked={choices[key]}
                        disabled={save.isPending}
                        onChange={() =>
                          setDraft({
                            ...draft,
                            [folder]: toggleSegment(draft[folder], medium, key),
                          })
                        }
                      />
                      {label}
                    </label>
                  ))}
                </div>
              </div>
            ) : (
              <p className="notice">
                This folder pattern is custom. Choose a layout to edit its
                fields.
              </p>
            )}
            <div className="naming-path-editor">
              <div className="naming-lane-heading">
                <h3>Field order</h3>
                <span>Drag to rearrange</span>
                <button
                  type="button"
                  className="naming-text-button"
                  aria-expanded={showJoins}
                  disabled={save.isPending || draft.layout !== "conventional"}
                  onClick={() => setShowJoins((open) => !open)}
                >
                  {showJoins
                    ? "Hide dash and folder options"
                    : "Adjust dashes and folders"}
                </button>
              </div>
              {showJoins && (
                <p className="naming-section-note">
                  A slash gives that field its own folder. A dash or a space
                  joins it to the next field. Parentheses wrap it, as in (1997).
                </p>
              )}
              {folders.error && (
                <p className="muted">
                  Library folder unavailable. The preview uses an example root.
                </p>
              )}
              <TokenLane
                template={draft[folder]}
                label="Folder token order"
                allowFolder
                showJoins={showJoins}
                disabled={save.isPending || draft.layout !== "conventional"}
                onChange={(value) => setDraft({ ...draft, [folder]: value })}
              />
            </div>
          </fieldset>
          <fieldset className="naming-block">
            <legend>File name</legend>
            <p className="naming-section-note">
              This is the name of the imported file, inside the last folder.
            </p>
            <div className="naming-file-controls">
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={draft.rename_files}
                  disabled={save.isPending}
                  onChange={(event) =>
                    setDraft({ ...draft, rename_files: event.target.checked })
                  }
                />
                Rename imported files
                <SettingHelp label="renaming files">
                  Applies to both ebooks and audiobooks. Hardlinks and copies
                  leave the torrent filenames in place for seeding and use this
                  pattern for the library file. Renaming the seeding copy uses
                  this pattern for that same file. Multi-file audiobooks need
                  disc and track in the filename so playback order is preserved.
                </SettingHelp>
              </label>
            </div>
            {draft.rename_files ? (
              <>
                {!styles.some(
                  (style) => style.template === draft[filename],
                ) && (
                  <p className="naming-section-note">
                    This file name uses a custom pattern. Choose a style to
                    replace it.
                  </p>
                )}
                <div className="naming-choice-grid">
                  {styles.map((style) => (
                    <ChoiceButton
                      key={style.name}
                      name={style.name}
                      example={fileExample(style.template)}
                      wide={style.name === "Disc and track"}
                      pressed={draft[filename] === style.template}
                      disabled={save.isPending}
                      onClick={() => {
                        setShowJoins(false);
                        setDraft({ ...draft, [filename]: style.template });
                      }}
                    />
                  ))}
                </div>
                {filenameSegments.length > 1 && (
                  <div className="naming-path-editor">
                    <div className="naming-lane-heading">
                      <h3>File name order</h3>
                      <span>Drag to rearrange</span>
                    </div>
                    <TokenLane
                      template={draft[filename]}
                      label="Filename token order"
                      allowFolder={false}
                      showJoins={showJoins}
                      disabled={save.isPending}
                      onChange={(value) =>
                        setDraft({ ...draft, [filename]: value })
                      }
                    />
                  </div>
                )}
              </>
            ) : (
              <p className="naming-section-note">
                Imported files keep the names they were downloaded with.
              </p>
            )}
          </fieldset>
        </div>
      </div>
      <Notice error={save.error || preview.error} />
      <div className="settings-form-footer">
        <div className="button-row">
          <button
            type="button"
            className="primary"
            disabled={
              !changed ||
              save.isPending ||
              !current ||
              !!preview.error ||
              !preview.data?.items.length ||
              preview.data.items.some((item) => item.state !== "ready")
            }
            onClick={() => save.mutate()}
          >
            {save.isPending ? "Saving…" : "Save naming settings"}
          </button>
          <button
            type="button"
            disabled={save.isPending}
            onClick={() => {
              setShowJoins(false);
              setDraft(defaults);
            }}
          >
            Reset naming defaults
          </button>
        </div>
        <span className="save-state" role="status">
          {changed ? "Unsaved changes" : "Saved"}
        </span>
      </div>
    </section>
  );
}

const tokenLabels: Record<string, string> = {
  author: "Author",
  title: "Title",
  series: "Series",
  sequence: "Sequence",
  recording_year: "Recording year",
  edition_year: "Edition year",
  narrator: "Narrator",
  edition: "Edition",
  language: "Language",
  publisher: "Publisher",
  disc: "Disc",
  track: "Track",
  year: "Year",
};
const joinLabels: [TokenJoin, string][] = [
  ["folder", "Own folder"],
  ["dash", "Dash"],
  ["space", "Space"],
  ["parentheses", "Parentheses"],
];
function TokenLane({
  template,
  label,
  disabled,
  allowFolder,
  showJoins,
  onChange,
}: {
  template: string;
  label: string;
  disabled: boolean;
  allowFolder: boolean;
  showJoins: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <div className="naming-token-lane">
      <Sortable
        horizontal
        label={label}
        disabled={disabled}
        values={templateSegments(template)}
        onChange={(segments) => onChange(segments.join(""))}
        render={(segment) => {
          const parsed = parseSegment(segment);
          const token =
            parsed?.token || segment.match(/\{([^}]+)\}/)?.[1] || "";
          const name =
            tokenLabels[token] || token.replaceAll("_", " ") || "Custom text";
          const join =
            showJoins && parsed?.join ? (
              <select
                className="naming-token-join"
                aria-label={`Join ${name} in ${label}`}
                value={parsed.join}
                disabled={disabled}
                onPointerDown={(event) => event.stopPropagation()}
                onChange={(event) => {
                  const next = withJoin(
                    segment,
                    event.target.value as TokenJoin,
                  );
                  onChange(
                    templateSegments(template)
                      .map((item) => (item === segment ? next : item))
                      .join(""),
                  );
                }}
              >
                {joinLabels
                  .filter(([value]) => allowFolder || value !== "folder")
                  .map(([value, joinName]) => (
                    <option key={value} value={value}>
                      {joinName}
                    </option>
                  ))}
              </select>
            ) : null;
          return (
            <>
              {parsed?.leading && join}
              <span className="naming-token-label">{name}</span>
              {parsed && !parsed.leading && join}
            </>
          );
        }}
      />
    </div>
  );
}

function ChoiceButton({
  name,
  example,
  pressed,
  disabled,
  wide = false,
  onClick,
}: {
  name: string;
  example: string;
  pressed: boolean;
  disabled: boolean;
  wide?: boolean;
  onClick: () => void;
}) {
  const nameId = useId();
  const exampleId = useId();
  return (
    <button
      type="button"
      className={`naming-choice${wide ? " naming-choice-wide" : ""}`}
      aria-pressed={pressed}
      aria-labelledby={nameId}
      aria-describedby={exampleId}
      disabled={disabled}
      onClick={onClick}
    >
      <span id={nameId} className="naming-choice-name">
        {name}
      </span>
      <span id={exampleId} className="naming-choice-example">
        {example}
      </span>
    </button>
  );
}
