import { useId, useState } from "react";
import { ReleaseDescription, ReleaseMediaInfo } from "./ReleaseContent";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import SourceReleaseDownload from "./SourceReleaseDownload";
import { transferSize } from "../pages/DownloadConstraints";
import BookDialog from "./BookDialog";

type Item = components["schemas"]["BookSearchView"]["items"][number];
export function ReleaseTags({ release }: { release: Item["release"] }) {
  return (
    <span className="release-tags">
      {release.source === "mam" &&
        (release.freeleech || release.personal_freeleech) && (
          <span className="release-tag freeleech">Freeleech</span>
        )}
      {release.source === "mam" && release.vip && (
        <span className="release-tag vip">VIP</span>
      )}
    </span>
  );
}
export default function SourceReleaseDetails({
  item,
  searchId,
  workId,
  rank,
  close,
  disabled,
  canAcquire,
  reviewContext,
}: {
  item: Item;
  searchId: string;
  workId: string;
  rank: number;
  close: () => void;
  disabled: boolean;
  canAcquire: boolean;
  reviewContext?: string;
}) {
  const [tab, setTab] = useState("Description");
  const [downloader, setDownloader] = useState("");
  const tabsId = useId();
  const tabs = ["Description", "Media info", "Details"];
  const original = item.release;
  const navigate = useNavigate();
  const [params] = useSearchParams();

  const clients = useQuery({
    queryKey: ["downloaders"],
    queryFn: async () => result(await api.GET("/api/downloaders")),
  });

  const qbitClients = (clients.data || []).filter(
    (client) => client.enabled && client.kind === "qbittorrent",
  );

  const inspect = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/source-searches/{search_id}/results/{result_id}/artifact",
          {
            params: {
              path: {
                search_id: searchId,
                result_id: item.id,
              },
            },
            body: downloader ? { downloader_id: downloader } : undefined,
          },
        ),
      ),
    onSuccess: (artifact) => {
      const context = new URLSearchParams(params);
      context.set("work", workId);
      navigate(
        `/sources/artifacts/${artifact.id}?${context.toString()}`,
      );
    },
  });
  const detail = useQuery({
    queryKey: ["mam-release", original.source_id],
    enabled: original.source === "mam" && item.current_connection,
    queryFn: async () =>
      result(
        await api.GET("/api/sources/mam/releases/{source_id}", {
          params: { path: { source_id: original.source_id } },
        }),
      ),
    staleTime: 60_000,
    retry: false,
  });
  const release = detail.data || original;
  const mam = release.source === "mam" ? release : null;
  return (
    <BookDialog
      title="Release details"
      close={close}
      className="release-dialog"
    >
      <article className="release-detail">
        <div className="release-detail-title">
          <span className="eyebrow">
            {release.source === "mam" ? "MAM" : release.source} · Rank{" "}
            {rank + 1}
          </span>
          <ReleaseTags release={release} />
        </div>
        <h2>{release.title}</h2>
        <p className="muted">
          {release.authors?.join(", ") || "Author not supplied"}
        </p>
        <dl className="release-decision-facts">
          <div>
            <dt>Format</dt>
            <dd>{release.formats?.join(", ").toUpperCase() || "Unknown"}</dd>
          </div>
          <div>
            <dt>Size</dt>
            <dd>
              {release.size_bytes == null
                ? "Unknown"
                : transferSize(release.size_bytes)}
            </dd>
          </div>
          <div>
            <dt>Availability</dt>
            <dd>
              {release.protocol === "soulseek"
                ? "Peer online"
                : `${release.seeders ?? "Unknown"} seeders`}
            </dd>
          </div>
          <div>
            <dt>Narrator</dt>
            <dd>{release.narrators?.join(", ") || "Not supplied"}</dd>
          </div>
        </dl>
        {!!item.assessment.blocked.length && (
          <p className="notice error">{item.assessment.blocked.join(" · ")}</p>
        )}
        <div className="release-detail-actions">
          {original.source === "prowlarr" && (
            <>
              {qbitClients.length > 0 && (
                <label>
                  qBittorrent for magnet inspection (optional)
                  <select
                    value={downloader}
                    onChange={(event) => setDownloader(event.target.value)}
                    disabled={inspect.isPending}
                  >
                    <option value="">Choose only if redirected to a magnet</option>
                    {qbitClients.map((client) => (
                      <option key={client.id} value={client.id}>
                        {client.name}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              <button
                type="button"
                disabled={!item.current_connection || !original.acquisition_supported || inspect.isPending}
                onClick={() => inspect.mutate()}
              >
                {inspect.isPending
                  ? "Inspecting…"
                  : original.protocol === "nzb"
                    ? "Inspect NZB"
                    : "Inspect torrent"}
              </button>
              {inspect.error && (
                <p className="notice error" role="alert">
                  {inspect.error instanceof Error
                    ? inspect.error.message
                    : "Could not inspect this release."}
                </p>
              )}
            </>
          )}
          {canAcquire && (
            <SourceReleaseDownload
              possibleCollection={item.possible_collection}
              reviewContext={reviewContext}
              searchId={searchId}
              resultId={item.id}
              source={original.source}
              title={release.title}
              disabled={disabled}
              download={item.download}
              showLabel
              offerWedge={
                original.source === "mam" &&
                !original.freeleech &&
                !original.personal_freeleech
              }
            />
          )}
          {mam && (
            <a
              href={`https://www.myanonamouse.net/t/${encodeURIComponent(mam.source_id)}`}
              target="_blank"
              rel="noreferrer"
            >
              Open release page <ExternalLink size={14} />
            </a>
          )}
        </div>
        {detail.isFetching && (
          <p className="muted" role="status">
            Loading full source details…
          </p>
        )}
        {detail.error && (
          <p className="notice">
            Could not refresh additional details. Showing the saved search result.{" "}
            <button onClick={() => detail.refetch()}>Retry details</button>
          </p>
        )}
        {mam && !!mam.tags?.length && (
          <div className="release-tags">
            {mam.tags!.map((tag) => (
              <span className="release-tag" key={tag}>
                {tag}
              </span>
            ))}
          </div>
        )}
        <div
          className="release-tabs"
          role="tablist"
          aria-label="Release information"
        >
          {tabs.map((name, index) => (
            <button
              key={name}
              id={`${tabsId}-tab-${index}`}
              role="tab"
              aria-selected={tab === name}
              aria-controls={`${tabsId}-panel-${index}`}
              tabIndex={tab === name ? 0 : -1}
              onClick={() => setTab(name)}
              onKeyDown={(event) => {
                const next =
                  event.key === "ArrowRight"
                    ? (index + 1) % tabs.length
                    : event.key === "ArrowLeft"
                      ? (index + tabs.length - 1) % tabs.length
                      : event.key === "Home"
                        ? 0
                        : event.key === "End"
                          ? tabs.length - 1
                          : null;
                if (next !== null) {
                  event.preventDefault();
                  setTab(tabs[next]);
                  document.getElementById(`${tabsId}-tab-${next}`)?.focus();
                }
              }}
            >
              {name}
            </button>
          ))}
        </div>
        <section
          className="release-tab-panel"
          role="tabpanel"
          tabIndex={0}
          id={`${tabsId}-panel-${tabs.indexOf(tab)}`}
          aria-labelledby={`${tabsId}-tab-${tabs.indexOf(tab)}`}
        >
          {tab === "Description" && (
            <ReleaseDescription text={release.description} />
          )}
          {tab === "Media info" && <ReleaseMediaInfo text={mam?.media_info} />}
          {tab === "Details" && (
            <>
              <dl className="release-facts">
                {Object.entries({
                  Format:
                    release.formats?.join(", ").toUpperCase() || "Unknown",
                  Medium:
                    release.medium === "audio"
                      ? "Audiobook"
                      : release.medium === "ebook"
                        ? "Ebook"
                        : "Unknown",
                  Size:
                    release.size_bytes == null
                      ? "Unknown"
                      : transferSize(release.size_bytes),
                  Seeds: release.seeders?.toLocaleString() ?? "Unknown",
                  Narrators: release.narrators?.join(", ") || "Not supplied",
                  Language: release.language || "Not supplied",
                  ...(mam
                    ? {
                        Leechers: mam.leechers?.toLocaleString() ?? "Unknown",
                        "Completed downloads":
                          mam.snatches?.toLocaleString() ?? "Unknown",
                        Uploaded: mam.uploaded_at || "Not supplied",
                        Category: mam.category || "Not supplied",
                        ISBN: mam.isbn || "Not supplied",
                        Series:
                          (mam.series || [])
                            .map(
                              (s) =>
                                `${s.name}${s.position ? ` · ${s.position}` : ""}`,
                            )
                            .join(", ") || "Not supplied",
                      }
                    : {}),
                }).map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            </>
          )}
        </section>
        {item.assessment.blocked.map((message) => (
          <p className="notice error" key={message}>
            {message}
          </p>
        ))}
        {!item.current_connection && (
          <p className="notice">
            This result expired or its source changed. Refresh the search.
          </p>
        )}
        {item.assessment.review.map((message) => (
          <p className="notice" key={message}>
            {message}
          </p>
        ))}
      </article>
    </BookDialog>
  );
}
