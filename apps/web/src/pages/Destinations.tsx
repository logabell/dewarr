import DeleteConfiguration from "../components/DeleteConfiguration";
import { useId, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  BookOpen,
  CheckCircle2,
  Copy,
  FolderOpen,
  Folder,
  Headphones,
  LoaderCircle,
} from "lucide-react";
import { api, result } from "../api/client";

import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import MountedFolderBrowser from "../components/MountedFolderBrowser";
import "./library-setup.css";
import BookDialog from "../components/BookDialog";
import AutomaticImportPolicy from "./AutomaticImportPolicy";
import {
  useLibraryFolderSettings,
  selectLibraryDestination,
} from "./libraryFolderSettings";
import { randomUUID } from "../randomUUID";

type Destination = components["schemas"]["DestinationView"];

function libraryApp(kind?: string) {
  return kind === "grimmory" ? "Grimmory" : "Audiobookshelf";
}

function posixPath(path: string) {
  return path.startsWith("/") && !path.startsWith("//");
}
type Medium = "ebook" | "audio";
const names = { ebook: "Ebooks", audio: "Audiobooks" };

export default function Destinations({
  embedded = false,
}: {
  embedded?: boolean;
}) {
  const [editing, setEditing] = useState<Medium | null>(null);
  const [verifying, setVerifying] = useState(false);
  const query = useLibraryFolderSettings();
  const downloaders = useQuery({
    queryKey: ["downloaders"],
    queryFn: async () => result(await api.GET("/api/downloaders")),
  });
  const readyClient = downloaders.data?.some(
    (item) =>
      item.enabled && item.status === "connected" && item.mappings_current,
  );
  const verifyFolder = (medium: Medium) => {
    setVerifying(true);
    setEditing(medium);
  };
  const selected = (medium: Medium) =>
    selectLibraryDestination(
      query.data?.destinations || [],
      query.data?.defaults.effective?.[`${medium}_destination_id`],
      medium,
    );
  return (
    <div className="library-folder-settings">
      {!embedded && <h1>Library folders</h1>}
      <p className="muted">
        Choose where completed ebooks and audiobooks belong. Downloads stay
        available for seeding.
      </p>
      <Notice error={query.error} />
      {query.isPending ? (
        <Loading />
      ) : (
        query.data && (
          <div className="media-folder-list">
            {(["ebook", "audio"] as const).map((medium) => {
              const destination = selected(medium);
              const library = query.data.libraries.find(
                (l) => l.id === destination?.library_id,
              );
              return (
                <section
                  className="media-folder-card"
                  key={medium}
                  aria-label={`${names[medium]} destination`}
                >
                  <div className="media-folder-row">
                    {medium === "ebook" ? (
                      <BookOpen size={21} />
                    ) : (
                      <Headphones size={21} />
                    )}
                    <div className="media-folder-copy">
                      <h3>{names[medium]}</h3>
                      <p>
                        {destination
                          ? `${library?.name || "Library"} · ${libraryApp(destination.server_kind)}`
                          : "No folder selected"}
                      </p>
                      {destination && (
                        <small
                          className={
                            destination.publication_available
                              ? "library-folder-status success"
                              : "library-folder-status muted"
                          }
                        >
                          {destination.publication_available &&
                          destination.seeding_rename ? (
                            <>
                              <CheckCircle2 size={12} /> Renames the seeding
                              copy
                            </>
                          ) : destination.publication_available &&
                            destination.mode === "hardlink" ? (
                            <>
                              <CheckCircle2 size={12} /> Hardlinks verified
                            </>
                          ) : destination.publication_available &&
                            destination.mode === "copy" ? (
                            "Copy mode · files are copied into this folder"
                          ) : (
                            "Folder saved · verification required"
                          )}
                        </small>
                      )}
                    </div>
                    <div className="media-folder-actions">
                      <button
                        onClick={() => {
                          setVerifying(false);
                          setEditing(medium);
                        }}
                        aria-label={`${destination ? "Change" : "Choose"} ${names[medium].toLowerCase()} folder`}
                      >
                        <Folder size={15} />
                        {destination ? "Change" : "Choose folder"}
                      </button>
                      {destination && (
                        <DeleteConfiguration
                          name={`${names[medium].toLowerCase()} folder`}
                          description="Remove this library folder configuration and turn off automatic imports into it. Existing books and files are kept."
                          onDelete={async () =>
                            result(
                              await api.DELETE(
                                "/api/organization/destinations/{destination_id}",
                                {
                                  params: {
                                    path: { destination_id: destination.id },
                                    query: {
                                      expected_revision: destination.revision,
                                    },
                                  },
                                },
                              ),
                            )
                          }
                          onDeleted={() => setEditing(null)}
                        />
                      )}
                    </div>
                  </div>
                  {destination && (
                    <div className="media-folder-automation">
                      <dl className="library-folder-paths">
                        <div>
                          <dt>
                            {libraryApp(destination.server_kind)} library folder
                          </dt>
                          <dd>
                            <code>{destination.backend_path}</code>
                          </dd>
                        </div>
                        {destination.local_path &&
                          destination.local_path !==
                            destination.backend_path && (
                            <div>
                              <dt>
                                Custom access path <span>used by Dewarr</span>
                              </dt>
                              <dd>
                                <code>{destination.local_path}</code>
                              </dd>
                            </div>
                          )}
                      </dl>
                      {!destination.publication_available && (
                        <section
                          className="library-verification-next"
                          aria-label="Folder verification"
                        >
                          <div>
                            <strong>
                              {readyClient
                                ? "Ready to verify"
                                : "Finish download setup"}
                            </strong>
                            <p>
                              {downloaders.isPending
                                ? "Checking download-client setup…"
                                : downloaders.isError
                                  ? "Could not check download-client setup. Try again to continue."
                                  : readyClient
                                    ? "Check file access before importing into this library."
                                    : "Connect a download client and its download folder, then verify this library."}
                            </p>
                          </div>
                          {downloaders.isError ? (
                            <button onClick={() => downloaders.refetch()}>
                              Retry setup check
                            </button>
                          ) : (
                            !downloaders.isPending &&
                            (readyClient ? (
                              <button
                                className="primary"
                                onClick={() => verifyFolder(medium)}
                              >
                                <CheckCircle2 size={15} /> Verify folder
                              </button>
                            ) : (
                              <Link
                                className="library-setup-link"
                                to="/settings#downloaders"
                              >
                                Set up download client
                              </Link>
                            ))
                          )}
                        </section>
                      )}
                      <AutomaticImportPolicy
                        destinationId={destination.id}
                        revision={destination.revision}
                        verified={destination.publication_available}
                        unsaved={false}
                        onVerify={() => verifyFolder(medium)}
                      />
                    </div>
                  )}
                </section>
              );
            })}
          </div>
        )
      )}
      {editing && (
        <FolderPicker
          medium={editing}
          saved={selected(editing)}
          verifying={verifying}
          close={() => setEditing(null)}
        />
      )}
    </div>
  );
}

function FolderPicker({
  medium,
  saved,
  verifying = false,
  close,
}: {
  medium: Medium;
  saved?: Destination;
  verifying?: boolean;
  close: () => void;
}) {
  const cache = useQueryClient();
  const current = useRef(saved);
  const formId = useId();
  const saveHelpId = useId();
  const browseButton = useRef<HTMLButtonElement>(null);
  const returnFromBrowser = () => {
    setBrowsing(false);
    requestAnimationFrame(() => browseButton.current?.focus());
  };
  const [browsing, setBrowsing] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(!!saved?.seeding_rename);
  const [choice, setChoice] = useState(
    saved ? `${saved.library_id}|${saved.backend_path}` : "",
  );
  const [localPath, setLocalPath] = useState(
    saved?.local_path && posixPath(saved.local_path)
      ? saved.local_path
      : saved?.backend_path && posixPath(saved.backend_path)
        ? saved.backend_path
        : "",
  );
  const [otherPath, setOtherPath] = useState(
    !!saved?.local_path && saved.local_path !== saved.backend_path,
  );
  const [verificationWarnings, setVerificationWarnings] = useState<string[]>(
    [],
  );
  const [seedingRename, setSeedingRename] = useState(!!saved?.seeding_rename);
  const [clientPath, setClientPath] = useState(saved?.client_path || "");
  const [automaticChoice, setAutomatic] = useState<boolean | null>(null);
  const policy = useQuery({
    queryKey: [
      "automatic-import-policy",
      saved?.id,
      saved?.revision,
      saved?.publication_available,
    ],
    enabled: !!saved,
    queryFn: async () =>
      result(
        await api.GET(
          "/api/organization/destinations/{destination_id}/automatic-import",
          { params: { path: { destination_id: saved!.id } } },
        ),
      ),
  });
  const automatic =
    automaticChoice ??
    policy.data?.requested_enabled ??
    (policy.data?.generation ? policy.data.enabled : true);
  const [progress, setProgress] = useState("");
  const options = useQuery({
    queryKey: ["library-folder-options"],
    queryFn: async () => {
      const [libraries, downloaders] = await Promise.all([
        api.GET("/api/organization/library-folders").then(result),
        api.GET("/api/downloaders").then(result),
      ]);
      return {
        libraries,
        downloaders: downloaders.filter(
          (d) => d.enabled && d.status === "connected" && d.mappings_current,
        ),
      };
    },
  });
  const clients = options.data?.downloaders || [];
  const qbit = clients.filter((client) => client.kind === "qbittorrent");
  const verificationClients = seedingRename ? qbit : clients;
  const canVerify = verificationClients.length > 0;
  const folders = (options.data?.libraries || [])
    .filter((item) =>
      medium === "audio" ? item.audio_allowed : item.ebooks_allowed,
    )
    .flatMap((item) =>
      item.folders.map((path) => ({
        ...item,
        path,
        key: `${item.library_id}|${path}`,
      })),
    )
    .sort((a, b) =>
      medium === "audio"
        ? Number(a.ebooks_allowed) - Number(b.ebooks_allowed)
        : 0,
    );
  const selectedChoice = choice || folders[0]?.key || "";
  const selectedFolder = folders.find(
    (folder) => folder.key === selectedChoice,
  );
  const library = selectedFolder;
  const backendPath = selectedFolder?.path || "";
  const remotePath = !!backendPath && !posixPath(backendPath);
  const mapping = otherPath || remotePath;
  const eligible = !!selectedFolder;
  const workerPath = mapping ? localPath.trim() : backendPath;
  const saveBlocker = !eligible
    ? "Choose an available library folder first."
    : !workerPath.trim()
      ? "Choose the mounted folder Dewarr can access."
      : seedingRename && !clientPath.trim()
        ? "Enter the library folder path in qBittorrent."
        : seedingRename && qbit.length !== 1
          ? "Seeding rename requires one configured qBittorrent client."
          : saved && !policy.data
            ? "Load the automatic import settings before saving this folder."
            : "";
  const save = useMutation({
    mutationFn: async () => {
      if (saveBlocker) throw new Error(saveBlocker);
      if (!library)
        throw new Error("Choose an available library folder first.");
      if (mapping && !posixPath(workerPath))
        throw new Error(
          "Enter the absolute folder Dewarr has mounted, such as /data/audiobooks.",
        );
      setVerificationWarnings([]);
      setProgress("Saving folder…");
      const destination = result(
        await api.PUT("/api/organization/library-folders/{medium}", {
          params: { path: { medium } },
          body: {
            library_id: library.library_id,
            backend_path: backendPath,
            local_path: workerPath,
            destination_id: current.current?.id,
            expected_revision: current.current?.revision,
            seeding_rename: seedingRename,
            client_path: seedingRename ? clientPath.trim() : null,
            automatic,
          },
        }),
      );
      current.current = destination;
      if (!canVerify) return { ...destination, warnings: [] as string[] };
      let verified = destination;
      let warnings: string[] = [];
      // A copy fallback changes the destination revision. Recheck the other
      // paths once under that final mode rather than retaining stale receipts.
      for (let pass = 0; pass < 2; pass++) {
        const revision = verified.revision;
        warnings = [];
        for (const client of verificationClients) {
          setProgress(
            `Checking ${client.name}'s download folder → ${library.library_name}…`,
          );
          try {
            const operation = result(
              await api.POST(
                "/api/organization/destinations/{destination_id}/setup-probe",
                {
                  params: {
                    path: { destination_id: destination.id },
                    header: { "idempotency-key": randomUUID() },
                  },
                  body: {
                    downloader_id: client.id,
                    downloader_generation: client.generation,
                    expected_revision: verified.revision,
                  },
                },
              ),
            );
            let completed = false;
            for (let attempt = 0; attempt < 80; attempt++) {
              await new Promise((resolve) => setTimeout(resolve, 1500));
              const activity = result(await api.GET("/api/activity"));
              const status = activity.find(
                (entry) => entry.id === operation.id,
              );
              if (
                status &&
                ["failed", "needs-review", "cancelled"].includes(status.status)
              )
                throw new Error(
                  status.message || "Folder verification failed.",
                );
              if (status?.status === "completed") {
                completed = true;
                break;
              }
            }
            if (!completed)
              throw new Error(
                "The worker has not finished checking this download folder.",
              );
          } catch (error) {
            warnings.push(
              `${client.name}: ${error instanceof Error ? error.message : "Folder verification failed."}`,
            );
          }
          verified =
            result(await api.GET("/api/organization/destinations")).find(
              (item) => item.id === destination.id,
            ) || verified;
          current.current = verified;
        }
        if (verified.revision === revision) break;
      }
      if (!verified.publication_available)
        throw new Error(warnings.join(" ") || "Folder verification failed.");
      setProgress("Setting your library destination…");
      const activated = result(
        await api.POST(
          "/api/organization/library-folders/{destination_id}/activate",
          {
            params: { path: { destination_id: destination.id } },
            body: { expected_revision: verified.revision, automatic },
          },
        ),
      );
      current.current = activated;
      return { ...activated, warnings };
    },
    onSuccess: async (destination) => {
      await Promise.all([
        cache.invalidateQueries({ queryKey: ["library-folder-settings"] }),
        cache.invalidateQueries({ queryKey: ["download-defaults"] }),
        cache.invalidateQueries({ queryKey: ["selection-options"] }),
        cache.invalidateQueries({ queryKey: ["automatic-import-policy"] }),
        cache.invalidateQueries({ queryKey: ["setup-readiness"] }),
      ]);
      if (destination.warnings.length)
        setVerificationWarnings(destination.warnings);
      else close();
    },
    onSettled: () =>
      cache.invalidateQueries({ queryKey: ["library-folder-settings"] }),
  });
  return (
    <BookDialog
      title={`${verifying ? "Verify" : "Choose"} ${names[medium].toLowerCase()} folder`}
      close={() => {
        if (!save.isPending) close();
      }}
      className="folder-picker-dialog library-setup-dialog"
    >
      <div className="library-setup-body">
        {browsing ? (
          <>
            <div className="library-browse-context">
              <span>Choose the mounted path for this library</span>
              <strong>
                {selectedFolder?.library_name} ·{" "}
                {libraryApp(selectedFolder?.server_kind)}
              </strong>
              <code>{backendPath}</code>
            </div>
            <MountedFolderBrowser
              purpose="library"
              selectLabel="Select folder"
              cancel={returnFromBrowser}
              select={(path) => {
                setChoice(selectedChoice);
                setOtherPath(true);
                setLocalPath(path);
                returnFromBrowser();
              }}
            />
          </>
        ) : (
          <>
            <p className="library-setup-intro">
              {verifying
                ? "Confirm these paths, then run verification to check file access and finish setting up imports."
                : "Choose the library for completed downloads. Use its default folder path or a custom path for your mount."}
            </p>
            <Notice error={options.error} />
            {options.isError && (
              <button type="button" onClick={() => options.refetch()}>
                Retry loading libraries
              </button>
            )}
            {options.isPending && <Loading />}
            {options.data && (
              <form
                id={formId}
                onSubmit={(event) => {
                  event.preventDefault();
                  save.mutate();
                }}
              >
                <fieldset
                  className="library-setup-options"
                  disabled={save.isPending}
                >
                  <legend>Library for {names[medium].toLowerCase()}</legend>
                  {folders.length > 1 && (
                    <label className="library-selection">
                      Library
                      <select
                        value={selectedChoice}
                        onChange={(event) => {
                          const folder = folders.find(
                            (item) => item.key === event.target.value,
                          );
                          if (!folder) return;
                          setChoice(folder.key);
                          setOtherPath(false);
                          setLocalPath(
                            posixPath(folder.path) ? folder.path : "",
                          );
                          setClientPath("");
                        }}
                      >
                        {!eligible && (
                          <option value={selectedChoice}>
                            Choose a library
                          </option>
                        )}
                        {[
                          ...new Set(
                            folders.map((folder) =>
                              libraryApp(folder.server_kind),
                            ),
                          ),
                        ].map((app) => (
                          <optgroup label={app} key={app}>
                            {folders
                              .filter(
                                (folder) =>
                                  libraryApp(folder.server_kind) === app,
                              )
                              .map((folder) => (
                                <option key={folder.key} value={folder.key}>
                                  {folder.library_name} — {folder.path}
                                </option>
                              ))}
                          </optgroup>
                        ))}
                      </select>
                    </label>
                  )}
                  {selectedFolder && (
                    <div className="library-selection-summary">
                      <p>
                        <strong>{selectedFolder.library_name}</strong>
                        <span>{libraryApp(selectedFolder.server_kind)}</span>
                      </p>
                      <code>{backendPath}</code>
                    </div>
                  )}
                  {!folders.length && (
                    <p className="notice">
                      {options.data.libraries.length
                        ? "No compatible folders found. Check your library server’s folder settings."
                        : "Connect Audiobookshelf or Grimmory to choose a library folder."}
                    </p>
                  )}
                  {options.data.libraries
                    .filter((item) => item.error)
                    .map((item) => (
                      <p className="notice error" key={item.library_id}>
                        {item.library_name}: {item.error}
                      </p>
                    ))}
                  {choice && !eligible && (
                    <p className="notice">
                      The saved library folder is no longer available. Choose
                      another library folder.
                    </p>
                  )}
                  {eligible && (
                    <fieldset
                      className="library-path-options"
                      disabled={save.isPending}
                    >
                      <legend>Folder path</legend>
                      <p className="library-path-help">
                        Where can Dewarr access this library?
                      </p>
                      <label className="library-path-option">
                        <input
                          type="radio"
                          name="library-path-mode"
                          checked={!mapping}
                          disabled={remotePath}
                          onChange={() => {
                            setOtherPath(false);
                          }}
                        />
                        <span>
                          <strong>
                            Use {libraryApp(selectedFolder?.server_kind)} path
                          </strong>
                          <small>
                            {remotePath
                              ? "This path needs a local mount. Use a custom path below."
                              : "Default · both apps access the same folder path."}
                          </small>
                        </span>
                      </label>
                      <label className="library-path-option">
                        <input
                          type="radio"
                          name="library-path-mode"
                          checked={mapping}
                          onChange={() => {
                            setChoice(selectedChoice);
                            setOtherPath(true);
                            if (!localPath && !remotePath)
                              setLocalPath(backendPath);
                          }}
                        />
                        <span>
                          <strong>Use a custom path</strong>
                          <small>
                            The same library folder, mounted at a different path
                            for Dewarr.
                          </small>
                        </span>
                      </label>
                      {mapping && (
                        <div className="library-custom-path">
                          <label>
                            Custom library path
                            <input
                              value={workerPath}
                              onChange={(event) => {
                                setChoice(selectedChoice);
                                setOtherPath(true);
                                setLocalPath(event.target.value);
                              }}
                              placeholder="/data/library/ebooks"
                              spellCheck={false}
                              autoCapitalize="none"
                            />
                          </label>
                          <button
                            type="button"
                            ref={browseButton}
                            onClick={() => setBrowsing(true)}
                          >
                            <FolderOpen size={15} aria-hidden="true" /> Browse
                            folders
                          </button>
                          <p className="library-mapping-help">
                            Select the existing folder visible to Dewarr. This
                            does not move your library or change its path in{" "}
                            {libraryApp(selectedFolder?.server_kind)}.
                          </p>
                        </div>
                      )}
                    </fieldset>
                  )}
                </fieldset>
                <section className="library-import-behavior">
                  <div className="library-local-heading">
                    <Copy size={19} aria-hidden="true" />
                    <div>
                      <h3>
                        {seedingRename
                          ? "Seeding files will be renamed"
                          : "Keep downloads available for seeding"}
                      </h3>
                      <p>
                        {seedingRename
                          ? "Dewarr will verify that qBittorrent can access the library folder."
                          : "Dewarr uses hardlinks when supported and copies files otherwise."}
                      </p>
                    </div>
                  </div>
                  <label className="library-toggle">
                    <input
                      type="checkbox"
                      checked={automatic}
                      disabled={save.isPending || (!!saved && !policy.data)}
                      onChange={(event) => setAutomatic(event.target.checked)}
                    />
                    <span>
                      <strong>Import on completion</strong>
                      <small>
                        Automatically add matched downloads to this library
                        after verification. Uncertain matches stay in review.
                      </small>
                    </span>
                  </label>
                </section>
                <p className="muted">
                  This is the final {medium === "audio" ? "audiobook" : "ebook"}{" "}
                  destination for all sources. Download clients use their own
                  download folders. Dewarr checks each connected client's file
                  access here; this does not change your client defaults.
                </p>
                {!options.data.downloaders.length && (
                  <p className="notice">
                    You can save this folder now. To verify it and enable
                    imports, connect and test a{" "}
                    <Link to="/settings#downloaders" onClick={close}>
                      download client
                    </Link>
                    , including its download folder.
                  </p>
                )}
                <details
                  className="library-advanced"
                  open={advancedOpen}
                  onToggle={(event) =>
                    setAdvancedOpen(event.currentTarget.open)
                  }
                >
                  <summary>Advanced · seeding file placement</summary>
                  <label className="library-toggle">
                    <input
                      type="checkbox"
                      checked={seedingRename}
                      disabled={
                        save.isPending || (!seedingRename && qbit.length !== 1)
                      }
                      onChange={(event) => {
                        setSeedingRename(event.target.checked);
                        if (event.target.checked && !clientPath.trim())
                          setClientPath(workerPath);
                      }}
                    />
                    <span>
                      <strong>Rename the seeding copy in qBittorrent</strong>
                      <small>
                        Optional. qBittorrent moves the seeding files into the
                        library; both use a single copy. Requires qBittorrent.
                      </small>
                    </span>
                  </label>
                  {seedingRename && (
                    <label className="library-client-path">
                      Library folder in qBittorrent
                      <input
                        value={clientPath}
                        onChange={(event) => setClientPath(event.target.value)}
                        placeholder={workerPath || "/data/library/ebooks"}
                        required
                        disabled={save.isPending}
                      />
                      <small>
                        Use the path qBittorrent sees for this same library
                        folder.
                      </small>
                    </label>
                  )}
                </details>
                {verificationWarnings.length > 0 && (
                  <div className="notice" role="alert">
                    <strong>
                      Library folder saved. Some download paths need attention.
                    </strong>
                    {verificationWarnings.map((warning) => (
                      <p key={warning}>{warning}</p>
                    ))}
                    <Link to="/settings#downloaders" onClick={close}>
                      Check download client folders
                    </Link>
                  </div>
                )}
                <Notice error={save.error || policy.error} />
                {save.isPending && (
                  <div role="status" className="library-verification-progress">
                    <LoaderCircle size={17} aria-hidden="true" />
                    <div>
                      <strong>{progress}</strong>
                      <p>
                        Keep this window open while Dewarr verifies access and
                        safe imports.
                      </p>
                    </div>
                  </div>
                )}
              </form>
            )}
          </>
        )}
      </div>
      {!browsing && options.data && (
        <footer className="library-setup-footer">
          <p id={saveHelpId} role="status">
            {saveBlocker ||
              (!canVerify
                ? "Save now; verify after setting up a download client."
                : "Save → verify connected download paths → activate")}
          </p>
          <div className="button-row">
            <button type="button" disabled={save.isPending} onClick={close}>
              Cancel
            </button>
            <button
              className="primary"
              type="submit"
              form={formId}
              aria-describedby={saveHelpId}
              disabled={save.isPending || !!saveBlocker}
            >
              {save.isPending
                ? canVerify
                  ? "Checking…"
                  : "Saving…"
                : canVerify
                  ? verifying
                    ? automatic
                      ? "Verify & enable imports"
                      : "Verify folder"
                    : "Save & verify folder"
                  : "Save folder"}
            </button>
          </div>
        </footer>
      )}
    </BookDialog>
  );
}
