import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "../components/InfiniteScroll";
import { EffectivePreferences } from "./PreferenceFields";
import { lazy, Suspense, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import {
  chooseRoute,
  destinationPreference,
  downloaderLabel,
  protocolPreference,
} from "./RouteFields";
import { randomUUID } from "../randomUUID";

type Artifact = components["schemas"]["SourceArtifactView"];
const ManualPack = lazy(() => import("./ManualPack"));

export default function ReleaseSelection({ artifact }: { artifact: Artifact }) {
  const [params] = useSearchParams();
  const cache = useQueryClient();
  const [choice, setChoice] = useState(
    params.get("request")
      ? `${params.get("request")}:${params.get("slot") || artifact.release.medium}`
      : "",
  );
  const [downloaderId, setDownloaderId] = useState("");
  const [destinationId, setDestinationId] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [group, setGroup] = useState<Array<{ id: string; title: string }>>([]);
  const [packId, setPackId] = useState<string | null>(null);
  const [packReceipt, setPackReceipt] = useState<
    components["schemas"]["ManualPackPrepared"] | null
  >(null);
  const key = useRef(randomUUID());
  const downloadKeys = useRef(new Map<string, string>());
  const options = useQuery({
    queryKey: ["selection-options"],
    queryFn: async () =>
      result(await api.GET("/api/acquisition/selections/options")),
  });
  const requests = usePagedQuery({
    queryKey: ["requests", "selection"],
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/requests", {
          signal,
          params: { query: { offset, limit: 20, mine: true } },
        }),
      ),
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.total ? count : undefined;
    },
  });
  const linkedRequest = useQuery({
    queryKey: ["requests", "selection-linked", params.get("request")],
    queryFn: async () =>
      result(
        await api.GET("/api/requests/{intent_id}", {
          params: { path: { intent_id: params.get("request")! } },
        }),
      ),
    enabled: !!params.get("request"),
  });
  const profiles = useQuery({
    queryKey: ["release-profiles"],
    queryFn: async () => result(await api.GET("/api/acquisition/profiles")),
  });
  const selectedProfile = profiles.data?.find(
    (p) => (p.id || "") === (params.get("profile") || ""),
  );
  const history = usePagedQuery({
    queryKey: ["release-selections", artifact.id],
    queryFn: async (historyOffset, signal) =>
      result(
        await api.GET("/api/acquisition/selections", {
          signal,
          params: {
            query: {
              artifact_id: artifact.id,
              offset: historyOffset,
              limit: 10,
            },
          },
        }),
      ),
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.total ? count : undefined;
    },
  });
  const choices = Array.from(
    new Map(
      [
        ...(requests.data?.items || []),
        ...(linkedRequest.data ? [linkedRequest.data] : []),
      ].map((item) => [item.id, item]),
    ).values(),
  ).flatMap((intent) =>
    intent.targets
      .filter(
        (target) =>
          target.state === "wanted" &&
          (target.slot === artifact.release.medium || target.slot === "either"),
      )
      .map((target) => ({
        intent,
        target,
        value: `${intent.id}:${target.slot}`,
      })),
  );
  const selected =
    choices.find((item) => item.value === choice) ||
    (!choice && params.get("work")
      ? choices.find((item) => item.intent.work_id === params.get("work"))
      : undefined);
  const useSearchContext = params.get("request")
    ? params.get("request") === selected?.intent.id
    : !params.get("work") || params.get("work") === selected?.intent.work_id;
  const downloaders =
    options.data?.downloaders.filter(
      (item) =>
        item.ready &&
        item.protocol ===
          (artifact.release.protocol === "nzb" ? "nzb" : "torrent"),
    ) || [];
  const preferences =
    selected?.intent.release_policy?.preferences ||
    selectedProfile?.preferences;
  const downloader = chooseRoute(
    downloaders,
    downloaderId,
    protocolPreference(
      preferences,
      artifact.release.protocol === "nzb" ? "nzb" : "torrent",
      downloaders,
    ),
  );
  const requiredLibrary =
    selected?.intent.specification[
      artifact.release.medium === "audio"
        ? "audio_library_id"
        : "ebook_library_id"
    ];
  const destinations =
    options.data?.destinations.filter(
      (item) =>
        item.ready &&
        item.medium === artifact.release.medium &&
        (item.source_keys || [item.source_key]).includes(
          downloader?.source_key || "",
        ) &&
        (!requiredLibrary || item.library_id === requiredLibrary),
    ) || [];
  const destination = chooseRoute(
    destinations,
    destinationId,
    destinationPreference(preferences, artifact.release.medium || "audio"),
  );
  const refresh = async () => {
    await Promise.all(
      [
        "requests",
        "release-selections",
        "selection-options",
        "downloads",
        "activity",
      ].map((name) => cache.invalidateQueries({ queryKey: [name] })),
    );
  };
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/acquisition/selections", {
          params: { header: { "idempotency-key": key.current } },
          body: {
            intent_id: selected!.intent.id,
            slot: selected!.target.slot as "audio" | "ebook" | "either",
            artifact_id: artifact.id,
            search_id: useSearchContext
              ? params.get("search") || undefined
              : undefined,
            downloader_id: downloader!.id,
            downloader_generation: downloader!.generation,
            destination_id: destination!.id,
            destination_revision: destination!.revision,
            confirmed_work_id: selected!.intent.work_id,
            ...(useSearchContext
              ? {
                  profile_id: params.get("profile") || undefined,
                  profile_effective_revision:
                    params.get("profile_effective_revision") ||
                    selectedProfile?.effective_revision ||
                    undefined,
                  profile_generation: params.get("profile_generation")
                    ? Number(params.get("profile_generation"))
                    : undefined,
                }
              : {}),
          },
        }),
      ),
    onSuccess: async () => {
      key.current = randomUUID();
      setConfirmed(false);
      await refresh();
    },
  });
  const cancel = useMutation({
    mutationFn: async (id: string) =>
      result(
        await api.DELETE("/api/acquisition/selections/{selection_id}", {
          params: { path: { selection_id: id } },
        }),
      ),
    onSuccess: refresh,
  });
  const startDownload = useMutation({
    mutationFn: async (ids: string[]) => {
      const sorted = [...ids].sort();
      const command = sorted.join(":");
      if (!downloadKeys.current.has(command))
        downloadKeys.current.set(command, randomUUID());
      return result(
        await api.POST("/api/acquisition/downloads", {
          params: {
            header: { "idempotency-key": downloadKeys.current.get(command)! },
          },
          body: {
            selection_id: sorted[0],
            additional_selection_ids: sorted.slice(1),
          },
        }),
      );
    },
    onSuccess: async () => {
      setGroup([]);
      setPackReceipt(null);
      setPackId(null);
      await refresh();
    },
  });
  const changed = () => {
    setConfirmed(false);
    key.current = randomUUID();
    save.reset();
  };
  return (
    <section
      className="panel editor library-access"
      aria-label="Release selection"
    >
      <h2>Select for a book request</h2>
      {selectedProfile && useSearchContext && (
        <EffectivePreferences
          preferences={selectedProfile.preferences}
          origins={selectedProfile.origins || {}}
        />
      )}
      <p>
        Download preferences:{" "}
        {!useSearchContext
          ? "Selected request’s saved preferences"
          : selectedProfile?.name ||
            (params.get("profile")
              ? "Saved preferences unavailable"
              : "Balanced")}
      </p>
      <p className="muted">
        Choose the book this release contains and its library destination. The
        whole torrent is selected; each book and version will need file
        inspection before import. Saving this choice does not start a download.
      </p>
      <Notice
        error={
          profiles.error ||
          options.error ||
          requests.error ||
          linkedRequest.error ||
          history.error ||
          save.error ||
          cancel.error ||
          startDownload.error
        }
      />
      <label>
        Wanted book
        <select
          value={selected?.value || choice}
          onChange={(event) => {
            changed();
            setChoice(event.target.value);
          }}
        >
          <option value="">Choose a saved request</option>
          {choices.map((item) => (
            <option key={item.value} value={item.value}>
              {item.intent.work_title} ·{" "}
              {item.target.slot === "audio"
                ? "Audiobook"
                : item.target.slot === "ebook"
                  ? "Ebook"
                  : "Either medium"}{" "}
              · {item.intent.description}
            </option>
          ))}
        </select>
      </label>
      <InfiniteScroll query={requests} />
      {!choices.length && !requests.isPending && (
        <p className="muted">
          No matching wanted requests.{" "}
          <Link to="/">Open a book and save the media you want first.</Link>
        </p>
      )}
      <label>
        Downloader
        <select
          value={downloader?.id || ""}
          onChange={(event) => {
            changed();
            setDownloaderId(event.target.value);
          }}
        >
          <option value="">Choose a tested downloader</option>
          {downloaders.map((item) => (
            <option key={item.id} value={item.id}>
              {downloaderLabel(item)}
            </option>
          ))}
        </select>
      </label>
      <label>
        Library destination
        <select
          value={destination?.id || ""}
          onChange={(event) => {
            changed();
            setDestinationId(event.target.value);
          }}
        >
          <option value="">Choose a verified library route</option>
          {destinations.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
      </label>
      {!options.isPending && (!downloaders.length || !destinations.length) && (
        <p className="muted">
          An administrator must test the downloader and verify its
          download-to-library route before a release can be selected.
        </p>
      )}
      {selected && (
        <label className="check-label">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => setConfirmed(event.target.checked)}
          />
          I checked the release details and it contains{" "}
          {selected.intent.work_title}.
        </label>
      )}
      <button
        className="primary"
        disabled={
          !artifact.current_connection ||
          !selectedProfile ||
          profiles.isFetching ||
          !selected ||
          !downloader ||
          !destination ||
          !confirmed ||
          save.isPending ||
          options.isFetching ||
          requests.isFetching
        }
        onClick={() => save.mutate()}
      >
        {save.isPending ? "Saving selection…" : "Save release selection"}
      </button>
      {save.isSuccess && (
        <p role="status" className="success">
          Release selection saved. No download has been started.
        </p>
      )}
      {!!history.data?.items.length && (
        <>
          <h3>Saved selections</h3>
          <p className="muted">
            For a series pack, review its additional books to prepare their
            selections together. You can also include existing selections below.
            They must use the same downloader and library route. File inspection
            confirms each book separately.
          </p>
          {packReceipt && (
            <div aria-live="polite">
              <p role="status">{packReceipt.message}</p>
              {packReceipt.records
                .filter((book) => !book.selection_id)
                .map((book) => (
                  <p key={book.work_id}>
                    {book.title}: {book.message}
                  </p>
                ))}
              <Link
                to={`/series/hardcover/${encodeURIComponent(packReceipt.external_id)}?request=${packReceipt.request_id}`}
              >
                View reviewed pack requests
              </Link>
            </div>
          )}
          {packId && (
            <Suspense fallback={<Loading />}>
              <ManualPack
                key={packId}
                selectionId={packId}
                onClose={() => setPackId(null)}
                onPrepared={async (value) => {
                  setPackReceipt(value);
                  setGroup(value.selections);
                  setPackId(null);
                  await refresh();
                }}
              />
            </Suspense>
          )}
          {group.length > 0 && (
            <section className="panel" aria-label="Shared download scope">
              <h4>{group.length} selected books · one transfer</h4>
              <ul>
                {group.map((item) => (
                  <li key={item.id}>{item.title}</li>
                ))}
              </ul>
              <button
                className="primary"
                disabled={group.length < 2 || startDownload.isPending}
                onClick={() =>
                  startDownload.mutate(group.map((item) => item.id))
                }
              >
                Download selected books together
              </button>
              <button
                disabled={startDownload.isPending}
                onClick={() => setGroup([])}
              >
                Clear selection
              </button>
            </section>
          )}
          {history.data.items.map((item) => (
            <article key={item.id} className="source-attribution">
              {item.dispatch_available && (
                <label>
                  <input
                    type="checkbox"
                    checked={group.some((entry) => entry.id === item.id)}
                    disabled={startDownload.isPending || cancel.isPending}
                    onChange={(event) => {
                      const checked = event.target.checked;
                      setGroup((current) =>
                        checked
                          ? [
                              ...current,
                              { id: item.id, title: item.work_title },
                            ]
                          : current.filter((entry) => entry.id !== item.id),
                      );
                    }}
                  />
                  Include {item.work_title} in shared download
                </label>
              )}
              {item.pack_review_available && (
                <button
                  disabled={startDownload.isPending || cancel.isPending}
                  onClick={() => {
                    setPackId(item.id);
                    setPackReceipt(null);
                  }}
                >
                  Review additional pack books
                </button>
              )}
              <span>
                <strong>{item.work_title}</strong>
                <small>{item.message}</small>
                {item.state === "prepared" && !item.configuration_current && (
                  <small>
                    Settings changed. Cancel this selection and review the
                    current route.
                  </small>
                )}
              </span>
              {item.dispatch_available && (
                <button
                  className="primary"
                  disabled={startDownload.isPending}
                  onClick={() => startDownload.mutate([item.id])}
                >
                  {startDownload.isPending
                    ? "Queuing download…"
                    : "Start download"}
                </button>
              )}
              {["committed", "fulfilled"].includes(item.state) && (
                <Link to="/requests?status=downloading">View download</Link>
              )}
              {item.state === "prepared" && (
                <button
                  disabled={cancel.isPending}
                  onClick={() => {
                    setGroup((current) =>
                      current.filter((entry) => entry.id !== item.id),
                    );
                    cancel.mutate(item.id);
                  }}
                >
                  Cancel selection
                </button>
              )}
            </article>
          ))}
          <InfiniteScroll query={history} />
        </>
      )}
    </section>
  );
}
