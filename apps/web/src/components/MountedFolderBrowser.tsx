import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Check,
  ChevronRight,
  Folder,
  HardDrive,
  Pencil,
  RefreshCw,
} from "lucide-react";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
import "./mounted-folders.css";

export default function MountedFolderBrowser({
  purpose,
  select,
  cancel,
  selectLabel = "Select folder",
}: {
  purpose: "download" | "library";
  select: (path: string) => void;
  cancel: () => void;
  selectLabel?: string;
}) {
  const [history, setHistory] = useState<(string | undefined)[]>([undefined]);
  const [index, setIndex] = useState(0);
  const [selected, setSelected] = useState<string>();
  const [editing, setEditing] = useState(false);
  const [enteredPath, setEnteredPath] = useState("");
  const list = useRef<HTMLDivElement>(null);
  const path = history[index];
  const endpoint =
    purpose === "library"
      ? "/api/organization/library-folders/browse"
      : "/api/downloaders/folders";
  const volumes = useQuery({
    queryKey: ["mounted-folders", purpose, undefined],
    queryFn: async () => result(await api.GET(endpoint)),
    staleTime: 0,
  });
  const folders = useQuery({
    queryKey: ["mounted-folders", purpose, path],
    queryFn: async () =>
      result(await api.GET(endpoint, { params: { query: { path } } })),
    staleTime: 0,
  });
  const navigate = (next?: string) => {
    if (next !== path) {
      setHistory((current) => [...current.slice(0, index + 1), next]);
      setIndex(index + 1);
    }
    setSelected(undefined);
    setEditing(false);
  };
  const travel = (next: number) => {
    setIndex(next);
    setSelected(undefined);
    setEditing(false);
  };
  const root = volumes.data?.directories.find(
    (volume) => path === volume || path?.startsWith(`${volume}/`),
  );
  const segments =
    root && path
      ? [root, ...path.slice(root.length).split("/").filter(Boolean)]
      : [];
  const selection =
    !folders.isError && !folders.isFetching
      ? selected && folders.data?.directories.includes(selected)
        ? selected
        : folders.data?.path
      : undefined;
  return (
    <section
      className="mounted-folder-browser"
      aria-label="Folders visible to Dewarr"
      onKeyDown={(event) => {
        if (
          (event.metaKey || event.ctrlKey) &&
          event.key.toLowerCase() === "l"
        ) {
          event.preventDefault();
          setEnteredPath(path || "");
          setEditing(true);
        } else if (event.altKey && event.key === "ArrowLeft" && index > 0) {
          event.preventDefault();
          travel(index - 1);
        } else if (
          event.altKey &&
          event.key === "ArrowRight" &&
          index < history.length - 1
        ) {
          event.preventDefault();
          travel(index + 1);
        }
      }}
    >
      <div
        className="mounted-folder-toolbar"
        role="toolbar"
        aria-label="Folder navigation"
      >
        <div className="mounted-folder-history">
          <button
            type="button"
            aria-label="Back in folders"
            title="Back"
            disabled={index === 0}
            onClick={() => travel(index - 1)}
          >
            <ArrowLeft size={17} />
          </button>
          <button
            type="button"
            aria-label="Forward in folders"
            title="Forward"
            disabled={index === history.length - 1}
            onClick={() => travel(index + 1)}
          >
            <ArrowRight size={17} />
          </button>
          <button
            type="button"
            aria-label="Up one level"
            title="Up one level"
            disabled={!path || !folders.data || folders.isFetching}
            onClick={() => navigate(folders.data?.parent ?? undefined)}
          >
            <ArrowUp size={17} />
          </button>
        </div>
        {editing ? (
          <form
            className="mounted-folder-address"
            onSubmit={(event) => {
              event.preventDefault();
              navigate(enteredPath.trim() || undefined);
            }}
          >
            <input
              autoFocus
              aria-label="Folder path"
              value={enteredPath}
              onChange={(event) => setEnteredPath(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  event.preventDefault();
                  event.stopPropagation();
                  setEditing(false);
                }
              }}
              placeholder="/data/library"
            />
            <button
              type="submit"
              aria-label="Go to folder"
              title="Go to folder"
            >
              <ArrowRight size={16} />
            </button>
          </form>
        ) : (
          <nav
            className="mounted-folder-breadcrumbs"
            aria-label="Current folder"
          >
            <button type="button" onClick={() => navigate()}>
              <HardDrive size={15} /> Volumes
            </button>
            {segments.map((segment, position) => {
              const location = segments.slice(0, position + 1).join("/");
              return (
                <span key={location}>
                  <ChevronRight size={13} />
                  <button
                    type="button"
                    aria-current={location === path ? "location" : undefined}
                    onClick={() => navigate(location)}
                  >
                    {segment}
                  </button>
                </span>
              );
            })}
            {path && !root && (
              <span className="mounted-folder-unknown">{path}</span>
            )}
          </nav>
        )}
        <button
          type="button"
          aria-label="Enter folder path"
          title="Enter folder path"
          onClick={() => {
            setEnteredPath(path || "");
            setEditing(true);
          }}
        >
          <Pencil size={16} />
        </button>
        <button
          type="button"
          aria-label="Refresh folders"
          title="Refresh folders"
          disabled={folders.isFetching}
          onClick={() => {
            setSelected(undefined);
            folders.refetch();
            if (path) volumes.refetch();
          }}
        >
          <RefreshCw size={16} />
        </button>
      </div>
      <div className="mounted-folder-workspace">
        <aside className="mounted-folder-sidebar" aria-label="Mounted volumes">
          <span>Locations</span>
          <button
            type="button"
            className={!path ? "active" : ""}
            onClick={() => navigate()}
          >
            <HardDrive size={16} /> All volumes
          </button>
          {volumes.data?.directories.map((volume) => (
            <button
              key={volume}
              type="button"
              className={root === volume ? "active" : ""}
              onClick={() => navigate(volume)}
              title={volume}
            >
              <HardDrive size={16} />
              <span>{volume}</span>
            </button>
          ))}
        </aside>
        <div className="mounted-folder-main">
          <div className="mounted-folder-columns">
            <span>Name</span>
            <span>Kind</span>
          </div>
          <Notice error={folders.error} />
          <div
            className="mounted-folder-contents"
            ref={list}
            aria-busy={folders.isFetching}
            aria-label="Folder list"
            onKeyDown={(event) => {
              const rows = Array.from(
                list.current?.querySelectorAll<HTMLButtonElement>(
                  "button[data-folder]",
                ) || [],
              );
              const current = rows.indexOf(
                document.activeElement as HTMLButtonElement,
              );
              const next =
                event.key === "ArrowDown"
                  ? Math.min(current + 1, rows.length - 1)
                  : event.key === "ArrowUp"
                    ? Math.max(current - 1, 0)
                    : event.key === "Home"
                      ? 0
                      : event.key === "End"
                        ? rows.length - 1
                        : -1;
              if (next >= 0 && rows[next]) {
                event.preventDefault();
                rows[next].focus();
                setSelected(rows[next].dataset.folder);
              }
            }}
          >
            {folders.isPending ? (
              <Loading />
            ) : (
              folders.data && (
                <>
                  <ul className="mounted-folder-list">
                    {folders.data.directories.map((folder) => (
                      <li key={folder}>
                        <button
                          type="button"
                          data-folder={folder}
                          aria-label={path ? folder.split("/").pop() : folder}
                          aria-pressed={selected === folder}
                          onClick={() => setSelected(folder)}
                          onDoubleClick={() => navigate(folder)}
                          onKeyDown={(event) => {
                            if (
                              !event.altKey &&
                              (event.key === "Enter" ||
                                event.key === "ArrowRight")
                            ) {
                              event.preventDefault();
                              navigate(folder);
                            }
                          }}
                        >
                          {path ? (
                            <Folder size={19} />
                          ) : (
                            <HardDrive size={19} />
                          )}
                          <span>{path ? folder.split("/").pop() : folder}</span>
                          <small>{path ? "Folder" : "Volume"}</small>
                        </button>
                      </li>
                    ))}
                  </ul>
                  {!folders.data.directories.length && (
                    <p className="mounted-folder-empty muted">
                      {path
                        ? "This folder is empty. You can select it below."
                        : "No readable volumes found. Mount your media in Dewarr, then refresh."}
                    </p>
                  )}
                  {folders.data.truncated && (
                    <p className="muted">
                      Showing 500 folders. Enter a more specific path to
                      continue.
                    </p>
                  )}
                </>
              )
            )}
          </div>
          <div className="mounted-folder-status">
            <span>{folders.data?.directories.length ?? 0} folders</span>
            <span>Double-click or press Enter to open</span>
          </div>
        </div>
      </div>
      <p className="mounted-folder-hint">
        Folders mounted in Dewarr · your computer’s local folders are not shown.
      </p>
      <footer className="mounted-folder-footer">
        <div className="mounted-folder-selection">
          <span>Folder</span>
          <code>{selection || "Choose a folder"}</code>
        </div>
        <div className="mounted-folder-footer-actions">
          <button type="button" onClick={cancel}>
            Cancel
          </button>
          <button
            type="button"
            disabled={!selected || folders.isFetching || folders.isError}
            onClick={() => navigate(selected)}
          >
            Open
          </button>
          <button
            type="button"
            className="primary"
            disabled={!selection}
            onClick={() => {
              if (selection) select(selection);
            }}
          >
            <Check size={15} />
            {selectLabel}
          </button>
        </div>
      </footer>
    </section>
  );
}
