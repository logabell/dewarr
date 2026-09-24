import ListBookPicker from "./ListBookPicker";
import { EffectiveScope } from "./ScopeFields";
import PreferenceFields, {
  EffectivePreferences,
  type Overrides,
} from "./PreferenceFields";
import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import DownloadConstraints from "./DownloadConstraints";
import {
  chooseRoute,
  primaryDownloaderPreference,
  destinationPreference,
  downloaderLabel,
} from "./RouteFields";
import { randomUUID } from "../randomUUID";

type Policy = components["schemas"]["ListPolicyView"];
type Input = components["schemas"]["ListPolicyInput"];

export default function ListPolicy({
  listId,
  follow = false,
}: {
  listId: string;
  follow?: boolean;
}) {
  const cache = useQueryClient();
  const path = { list_id: listId };
  const policy = useQuery({
    queryKey: ["list-policy", listId],
    queryFn: async () =>
      result(
        await api.GET("/api/lists/{list_id}/acquisition", { params: { path } }),
      ),
    refetchInterval: (query) =>
      query.state.data?.active &&
      query.state.data.configuration.mode === "automatic"
        ? 5000
        : false,
  });
  const [offset, setOffset] = useState(0);
  const books = useQuery({
    queryKey: ["list-policy-books", listId, offset],
    queryFn: async () =>
      result(
        await api.GET("/api/lists/{list_id}/acquisition/books", {
          params: { path, query: { offset, limit: 25 } },
        }),
      ),
    refetchInterval:
      policy.data?.active && policy.data.configuration.mode === "automatic"
        ? 5000
        : false,
  });
  const saved = (value: Policy) => {
    cache.setQueryData(["list-policy", listId], value);
    void cache.invalidateQueries({ queryKey: ["following"] });
    void cache.invalidateQueries({ queryKey: ["list-policy-books", listId] });
  };
  const pause = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/lists/{list_id}/acquisition/pause", {
          params: { path },
          body: { expected_revision: policy.data!.revision },
        }),
      ),
    onSuccess: saved,
  });
  return (
    <section className="panel editor" aria-label="List acquisition policy">
      <h2>{follow ? "Follow acquisition" : "List acquisition"}</h2>
      <p>
        Keep browsing, request books yourself, or automatically acquire missing
        media when{" "}
        {follow ? "matching books are discovered" : "books join this list"}.
      </p>
      <Notice error={policy.error || books.error || pause.error} />
      {policy.isPending ? (
        <Loading />
      ) : policy.isSuccess ? (
        <>
          {policy.data && (
            <>
              <p role="status">
                {policy.data.active
                  ? policy.data.message
                  : "Acquisition paused; list synchronization continues"}
              </p>
              {policy.data.active &&
                policy.data.configuration.mode === "automatic" && (
                  <button
                    onClick={() => pause.mutate()}
                    disabled={pause.isPending}
                  >
                    Pause automatic acquisition
                  </button>
                )}
            </>
          )}
          <PolicyEditor
            key={`${listId}:${policy.data?.revision || 0}`}
            listId={listId}
            follow={follow}
            policy={policy.data}
            saved={saved}
          />
        </>
      ) : null}
      {!!books.data?.items.length && (
        <details>
          <summary>Monitored books and backlog</summary>
          {books.data.items.map((book) => (
            <article className="source-attribution" key={book.id}>
              <div>
                <Link to={`/books/${book.work_id}`}>{book.title}</Link>
                <p>
                  {book.state} · {book.message}
                </p>
                {book.series_request_id && book.series_external_id && (
                  <Link
                    to={`/series/hardcover/${book.series_external_id}?request=${book.series_request_id}`}
                  >
                    Open list-derived series request
                  </Link>
                )}
                {book.series_scope_issue?.external_id && (
                  <Link
                    to={`/series/hardcover/${book.series_scope_issue.external_id}`}
                  >
                    Review main-series books
                  </Link>
                )}
                {book.next_check_at && (
                  <small>
                    Next check: {new Date(book.next_check_at).toLocaleString()}
                  </small>
                )}
              </div>
            </article>
          ))}
          <div className="button-row">
            {offset > 0 && (
              <button onClick={() => setOffset(offset - 25)}>
                Previous monitored books
              </button>
            )}
            {offset + 25 < books.data.total && (
              <button onClick={() => setOffset(offset + 25)}>
                Next monitored books
              </button>
            )}
          </div>
        </details>
      )}
    </section>
  );
}

function PolicyEditor({
  listId,
  follow,
  policy,
  saved,
}: {
  listId: string;
  follow: boolean;
  policy: Policy | null;
  saved: (value: Policy) => void;
}) {
  const [mode, setMode] = useState<Input["mode"]>(
    (policy?.configuration.mode as Input["mode"]) || "browse",
  );
  const [medium, setMedium] = useState<Input["specification"]["mode"]>(
    policy?.configuration.scope_options?.mode ||
      policy?.configuration.preference_overrides?.desired_media ||
      (policy?.configuration.scope_options == null
        ? policy?.configuration.specification.mode
        : undefined) ||
      null,
  );
  const [preferred, setPreferred] = useState<"ebook" | "audio">(
    policy?.configuration.specification.preferred_medium || "audio",
  );
  const [overrides, setOverrides] = useState<Overrides>(() => {
    const values = {
      ...(policy?.configuration.preference_overrides ??
        policy?.configuration.profile.list_overrides ??
        {}),
      ...Object.fromEntries(
        Object.entries(policy?.configuration.scope_options || {}).filter(
          ([key]) =>
            [
              "language",
              "abridged",
              "standalone",
              "ebook_library_id",
              "audio_library_id",
            ].includes(key),
        ),
      ),
    };
    delete values.desired_media;
    delete values.preferred_medium;
    return values;
  });
  const [profileId, setProfileId] = useState(
    policy?.configuration.profile.id || "",
  );
  const [downloaderId, setDownloaderId] = useState(
    (policy?.configuration.route_options || policy?.configuration)
      ?.downloader_id || "",
  );
  const [destinations, setDestinations] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      Object.entries(
        (policy?.configuration.route_options || policy?.configuration)
          ?.routes || {},
      ).map(([m, r]) => [m, r.destination_id]),
    ),
  );
  const [selected, setSelected] = useState<string[]>([]);
  const [selectionValid, setSelectionValid] = useState(false);
  const [contentRevision, setContentRevision] = useState<string>();
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const key = useRef(randomUUID());
  const path = { list_id: listId };
  const options = useQuery({
    queryKey: ["selection-options"],
    enabled: mode === "automatic",
    queryFn: async () =>
      result(await api.GET("/api/acquisition/selections/options")),
  });
  const profiles = useQuery({
    queryKey: ["release-profiles"],
    queryFn: async () => result(await api.GET("/api/acquisition/profiles")),
  });
  const downloaders = options.data?.downloaders.filter((d) => d.ready) || [];
  const profile = profiles.data?.find((p) => (p.id || "") === profileId);
  const preferences = { ...profile?.preferences, ...overrides };
  const downloader = chooseRoute(
    downloaders,
    downloaderId,
    primaryDownloaderPreference(preferences, downloaders),
  );
  const effectiveMedium =
    medium || overrides.desired_media || profile?.preferences.desired_media;
  const media =
    effectiveMedium === "both" || effectiveMedium === "either"
      ? (["ebook", "audio"] as const)
      : effectiveMedium
        ? [effectiveMedium]
        : [];
  const available = (m: string) =>
    options.data?.destinations.filter(
      (d) =>
        d.medium === m &&
        d.ready &&
        d.automatic_import_ready &&
        d.source_key === downloader?.source_key,
    ) || [];
  const destination = (m: string) =>
    chooseRoute(
      available(m),
      destinations[m],
      destinationPreference(preferences, m),
    );
  const specification: Input["specification"] = {
    mode: medium || undefined,
    ...(medium === "either" ? { preferred_medium: preferred } : {}),
    download_constraints: policy?.configuration.request_constraints || null,
  };
  if (medium !== "either") delete specification.preferred_medium;
  const input: Input = {
    mode,
    specification,
    profile_id: profile?.id || null,
    profile_generation: profile?.generation || 0,
    profile_effective_revision: profile?.effective_revision,
    preference_overrides: overrides,
    expected_revision: policy?.revision || 0,
    include_work_ids: mode === "automatic" ? selected : [],
    expected_content_revision:
      mode === "automatic" && selected.length ? contentRevision : undefined,
    downloader_id:
      mode === "automatic" &&
      (downloaderId || !primaryDownloaderPreference(preferences, downloaders))
        ? downloader?.id
        : null,
    downloader_generation:
      mode === "automatic" &&
      (downloaderId || !primaryDownloaderPreference(preferences, downloaders))
        ? downloader?.generation
        : null,
    routes:
      mode === "automatic"
        ? Object.fromEntries(
            media.flatMap((m) => {
              if (!destinations[m] && destinationPreference(preferences, m))
                return [];
              const d = destination(m);
              return d
                ? [
                    [
                      m,
                      {
                        destination_id: d.id,
                        destination_revision: d.revision,
                      },
                    ],
                  ]
                : [];
            }),
          )
        : {},
  };
  const preview = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/lists/{list_id}/acquisition/preview", {
          params: { path, header: { "idempotency-key": key.current } },
          body: input,
        }),
      ),
    onSuccess: (value) => {
      setPreviewId(value.id);
      setOffset(0);
    },
  });
  const receipt = useQuery({
    queryKey: ["list-policy-preview", listId, previewId, offset],
    enabled: !!previewId,
    queryFn: async () =>
      result(
        await api.GET(
          "/api/lists/{list_id}/acquisition/previews/{identifier}",
          {
            params: {
              path: { ...path, identifier: previewId! },
              query: { offset, limit: 50 },
            },
          },
        ),
      ),
  });
  const activate = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/lists/{list_id}/acquisition/previews/{identifier}/activate",
          { params: { path: { ...path, identifier: previewId! } } },
        ),
      ),
    onSuccess: saved,
  });
  const changed = () => {
    key.current = randomUUID();
    preview.reset();
  };
  return (
    <>
      <Notice
        error={
          options.error ||
          profiles.error ||
          preview.error ||
          receipt.error ||
          activate.error
        }
      />
      {!previewId ? (
        <form
          aria-label="List policy settings"
          onSubmit={(event) => {
            event.preventDefault();
            preview.mutate();
          }}
        >
          <fieldset disabled={preview.isPending}>
            <label>
              Acquisition mode
              <select
                value={mode}
                onChange={(e) => {
                  setMode(e.target.value as Input["mode"]);
                  changed();
                }}
              >
                <option value="browse">Browse only</option>
                <option value="manual">Manual requests</option>
                <option value="automatic">Automatic acquisition</option>
              </select>
            </label>
            <label>
              Download profile
              <select
                value={profileId}
                onChange={(e) => {
                  setProfileId(e.target.value);
                  changed();
                }}
              >
                {profiles.data?.map((p) => (
                  <option key={p.id || "default"} value={p.id || ""}>
                    {p.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Desired media
              <select
                value={medium || ""}
                onChange={(e) => {
                  setMedium((e.target.value || null) as typeof medium);
                  changed();
                }}
              >
                <option value="">Use my download settings</option>
                <option value="ebook">Ebook</option>
                <option value="audio">Audiobook</option>
                <option value="both">Both</option>
                <option value="either">Either medium</option>
              </select>
            </label>
            {medium === "either" && (
              <label>
                Search first
                <select
                  value={preferred}
                  onChange={(e) => {
                    setPreferred(e.target.value as typeof preferred);
                    changed();
                  }}
                >
                  <option value="audio">Audiobook</option>
                  <option value="ebook">Ebook</option>
                </select>
              </label>
            )}
            {profile && (
              <details>
                <summary>List download overrides</summary>
                <PreferenceFields
                  includeMedia={false}
                  overrides={overrides}
                  inherited={profile.preferences}
                  origins={profile.origins || {}}
                  onChange={(value) => {
                    changed();
                    setOverrides(value);
                  }}
                />
              </details>
            )}
            {mode === "automatic" && (
              <>
                <label>
                  Downloader
                  <select
                    value={downloader?.id || ""}
                    onChange={(e) => {
                      setDownloaderId(e.target.value);
                      changed();
                    }}
                  >
                    <option value="">Choose a tested downloader</option>
                    {downloaders.map((d) => (
                      <option key={d.id} value={d.id}>
                        {downloaderLabel(d)}
                      </option>
                    ))}
                  </select>
                </label>
                {media.map((m) => (
                  <label key={m}>
                    {m === "audio" ? "Audiobook" : "Ebook"} destination
                    <select
                      value={destination(m)?.id || ""}
                      onChange={(e) => {
                        setDestinations({
                          ...destinations,
                          [m]: e.target.value,
                        });
                        changed();
                      }}
                    >
                      <option value="">Choose an approved destination</option>
                      {available(m).map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
                <p className="muted">
                  Future additions are included. Existing books are excluded
                  unless selected below. Already-owned requested media are
                  skipped. Routes need administrator approval for automatic
                  importing.
                </p>
                <details>
                  <summary>
                    Include current books ({selected.length} selected, maximum
                    25)
                  </summary>
                  <ListBookPicker
                    listId={listId}
                    selected={selected}
                    onChange={(ids) => {
                      setSelected(ids);
                      changed();
                    }}
                    maximum={25}
                    label="Find current books"
                    onValidityChange={setSelectionValid}
                    onRevisionChange={setContentRevision}
                  />
                </details>
              </>
            )}
            <button
              className="primary"
              disabled={
                preview.isPending ||
                (mode === "automatic" &&
                  selected.length > 0 &&
                  !selectionValid) ||
                !profiles.isSuccess ||
                (!!profileId && !profile) ||
                (mode === "automatic" &&
                  (!downloader || media.some((m) => !destination(m))))
              }
            >
              {follow ? "Preview follow policy" : "Preview list policy"}
            </button>
          </fieldset>
        </form>
      ) : receipt.data ? (
        <div aria-label="List activation preview">
          <h3>Review {receipt.data.configuration.mode} mode</h3>
          <p>
            {receipt.data.counts?.owned || 0} owned ·{" "}
            {receipt.data.counts?.missing || 0} missing ·{" "}
            {receipt.data.counts?.excluded || 0} excluded
          </p>
          <EffectiveScope
            specification={receipt.data.configuration.specification}
            origins={receipt.data.configuration.profile.scope_origins}
          />
          <EffectivePreferences
            preferences={receipt.data.configuration.profile.preferences}
            origins={receipt.data.configuration.profile.origins || {}}
          />
          <p>
            {receipt.data.total} current books · {receipt.data.selected}{" "}
            selected for acquisition.
          </p>
          <p>
            Current books not selected remain in your list. Existing authorized
            work continues when you resume an unchanged policy. Removing a book
            withdraws only this list’s request.
          </p>
          <p>
            Preferences: {receipt.data.configuration.profile.name} ·{" "}
            {receipt.data.configuration.specification.mode}
          </p>
          <DownloadConstraints
            value={
              receipt.data.configuration.specification.download_constraints
            }
          />
          {receipt.data.records.map((r) => (
            <article className="source-attribution" key={r.work_id}>
              <div>
                <strong>{r.title}</strong>
                <p>
                  {r.selected ? "Selected" : "Not selected for backlog"} ·{" "}
                  {r.targets.map((t) => `${t.slot}: ${t.state}`).join(" · ")}
                </p>
                {r.series_scope && (
                  <div>
                    <p>{r.series_scope.message}</p>
                    {!!r.series_scope.records.length && (
                      <ul>
                        {r.series_scope.records.map((book) => (
                          <li key={book.work_id}>{book.title}</li>
                        ))}
                      </ul>
                    )}
                    {r.series_scope.external_id && (
                      <Link
                        to={`/series/hardcover/${r.series_scope.external_id}`}
                      >
                        Open series
                      </Link>
                    )}
                  </div>
                )}
              </div>
            </article>
          ))}
          <div className="button-row">
            {offset > 0 && (
              <button onClick={() => setOffset(offset - 50)}>
                Previous activation entries
              </button>
            )}
            {offset + 50 < receipt.data.total && (
              <button onClick={() => setOffset(offset + 50)}>
                Next activation entries
              </button>
            )}
            <button
              onClick={() => {
                setPreviewId(null);
                changed();
              }}
            >
              Edit list policy
            </button>
            <button
              className="primary"
              onClick={() => activate.mutate()}
              disabled={activate.isPending}
            >
              {receipt.data.configuration.mode === "automatic"
                ? "Activate automatic acquisition"
                : "Save list mode"}
            </button>
          </div>
        </div>
      ) : (
        <Loading />
      )}
    </>
  );
}
