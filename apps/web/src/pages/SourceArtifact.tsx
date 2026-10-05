import { safeReturn } from "../components/NavigationContinuity";
import InfiniteScroll from "../components/InfiniteScroll";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Link,
  useLocation,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import ReleaseSelection from "./ReleaseSelection";

type Descriptor = components["schemas"]["SourceArtifactView"]["descriptor"];

function isNzb(
  descriptor: Descriptor,
): descriptor is components["schemas"]["NzbDescriptor"] {
  return "protocol" in descriptor;
}

export default function SourceArtifact() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const location = useLocation();
  const workId = params.get("work");
  const bookContext = new URLSearchParams(params);
  bookContext.set("tab", "sources");
  const [page, setPage] = useState(0);
  const artifact = useQuery({
    queryKey: ["source-artifact", id],
    queryFn: async () =>
      result(
        await api.GET("/api/source-artifacts/{artifact_id}", {
          params: { path: { artifact_id: id } },
        }),
      ),
  });
  if (artifact.isPending) return <Loading />;
  if (!artifact.data) return <Notice error={artifact.error} />;
  const { descriptor, release, current_connection, created_at } = artifact.data;
  const usenet = isNzb(descriptor);
  const files = descriptor.files.slice(0, (page + 1) * 50);
  return (
    <>
      <header className="page-heading">
        <div>
          <p className="eyebrow">
            {release.source === "mam"
              ? "MAM"
              : release.source === "audiobookbay"
                ? "AUDIOBOOKBAY"
                : "PROWLARR"}{" "}
            RELEASE
          </p>
          <h1>{usenet ? "NZB manifest" : "Torrent manifest"}</h1>
          <p>{release.title}</p>
          <Link
            state={{
              returnTo: location.state?.bookOrigin,
              restoreScroll: true,
            }}
            to={safeReturn(
              location.state?.returnTo,
              workId
                ? `/books/${encodeURIComponent(workId)}?${bookContext}`
                : `/search?q=${encodeURIComponent(release.title)}`,
            )}
          >
            {workId ? "Return to book sources" : "Find book"}
          </Link>
        </div>
      </header>
      <Notice error={artifact.error} />
      {!current_connection && (
        <p className="notice error">
          The source connection changed. Inspect this release again before using
          it for a download.
        </p>
      )}
      <section
        className="panel"
        aria-label={usenet ? "Inspected NZB" : "Inspected torrent"}
      >
        <h2>{descriptor.name}</h2>
        <p>
          {descriptor.files.length} files ·{" "}
          {descriptor.content_bytes.toLocaleString()} bytes of content
          {isNzb(descriptor)
            ? ` · ${descriptor.nzb_bytes.toLocaleString()} byte NZB`
            : descriptor.private
              ? " · Private tracker"
              : " · Public torrent"}
        </p>
        <p className="muted">
          Inspected {new Date(created_at).toLocaleString()}.{" "}
          {usenet
            ? "These are the file names from the NZB. The Usenet client unpacks the download, and book matches are reviewed after it finishes."
            : "These are torrent file entries; book and edition matches are reviewed after download."}
        </p>
        <p className="notice">
          {usenet ? "NZB" : "Torrent"} metadata is saved privately. No download
          has been started.
        </p>
        <ul
          className="artifact-files"
          aria-label={usenet ? "NZB files" : "Torrent files"}
        >
          {files.map((file) => (
            <li key={file.index}>
              <span className="break-text">{file.path}</span>
              <span className="muted">
                {file.size_bytes.toLocaleString()} B
              </span>
            </li>
          ))}
        </ul>
        <InfiniteScroll
          query={{
            hasNextPage: (page + 1) * 50 < descriptor.files.length,
            isFetching: false,
            isFetchNextPageError: false,
            fetchNextPage: async () => setPage((n) => n + 1),
          }}
        />
        <details>
          <summary>{usenet ? "NZB identity" : "Torrent identity"}</summary>
          <dl className="source-facts">
            {isNzb(descriptor) ? (
              <>
                <dt>NZB checksum</dt>
                <dd className="break-text">{descriptor.artifact_sha256}</dd>
              </>
            ) : (
              <>
                <dt>v1 info hash</dt>
                <dd className="break-text">
                  {descriptor.infohash_v1 || "Not present"}
                </dd>
                <dt>v2 info hash</dt>
                <dd className="break-text">
                  {descriptor.infohash_v2 || "Not present"}
                </dd>
                <dt>Metadata checksum</dt>
                <dd className="break-text">{descriptor.artifact_sha256}</dd>
                <dt>Padding</dt>
                <dd>
                  {descriptor.padding_bytes.toLocaleString()} B (not library
                  content)
                </dd>
              </>
            )}
          </dl>
        </details>
      </section>
      <ReleaseSelection key={id} artifact={artifact.data} />
    </>
  );
}
