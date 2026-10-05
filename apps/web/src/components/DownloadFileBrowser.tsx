import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Folder, File } from "lucide-react";
import { api, result } from "../api/client";
import { Notice } from "../components";
import BookDialog from "./BookDialog";
import { transferSize } from "../pages/DownloadConstraints";

export default function DownloadFileBrowser({
  source,
  select,
  close,
}: {
  source: string;
  select: (path: string) => void;
  close: () => void;
}) {
  const [path, setPath] = useState("");
  const listing = useQuery({
    queryKey: ["download-files", source, path],
    queryFn: async ({ signal }) =>
      result(
        await api.GET("/api/organization/download-files", {
          params: { query: { source_key: source, path } },
          signal,
        }),
      ),
    staleTime: 10_000,
  });
  return (
    <BookDialog title="Choose completed files" close={close}>
      <p className="muted">
        {source} / {path || "Download root"}
      </p>
      <Notice error={listing.error} />
      {listing.isPending && <p role="status">Reading folder…</p>}
      <div className="actions">
        {path && (
          <button
            onClick={() => setPath(path.split("/").slice(0, -1).join("/"))}
          >
            ← Parent folder
          </button>
        )}
        {path && listing.isSuccess && (
          <button className="primary" onClick={() => select(path)}>
            Choose this folder
          </button>
        )}
        {listing.isError && (
          <button onClick={() => listing.refetch()}>Try again</button>
        )}
      </div>
      <ul className="download-browser-list">
        {listing.data?.entries.map((entry) => (
          <li key={entry.path}>
            <button
              onClick={() =>
                entry.kind === "directory"
                  ? setPath(entry.path)
                  : select(entry.path)
              }
            >
              {entry.kind === "directory" ? (
                <Folder size={18} />
              ) : (
                <File size={18} />
              )}
              <span>{entry.name}</span>
              <small>
                {entry.kind === "directory"
                  ? "Open folder →"
                  : entry.size == null
                    ? "Choose file"
                    : transferSize(entry.size)}
              </small>
            </button>
          </li>
        ))}
      </ul>
      {listing.isSuccess && !listing.data.entries.length && (
        <p>No accessible files or folders here.</p>
      )}
      {listing.data?.truncated && (
        <p className="notice">
          Showing the first 500 entries. Enter a relative path in the import
          form to select another file or folder.
        </p>
      )}
      <p className="muted">
        Only the selected download root is browsed. Symbolic links are excluded.
      </p>
    </BookDialog>
  );
}
