import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import RouteFields from "./RouteFields";
import { Link } from "react-router-dom";
import ScopeFields from "./ScopeFields";
import PreferenceFields from "./PreferenceFields";
import SettingHelp from "../components/SettingHelp";
type Defaults = components["schemas"]["DefaultsView"];
type Scope = "personal" | "installation";

export default function DownloadPreferences({
  admin,
  embedded = false,
  librariesOnly = false,
}: {
  admin: boolean;
  embedded?: boolean;
  librariesOnly?: boolean;
}) {
  const [saved, setSaved] = useState(false);
  const [scope, setScope] = useState<Scope>("personal");
  const current = useQuery({
    queryKey: ["download-defaults", scope],
    queryFn: async () =>
      result(
        await api.GET("/api/acquisition/preferences/{scope}", {
          params: { path: { scope } },
        }),
      ),
  });
  return (
    <section className="download-defaults" aria-label="Download defaults">
      {!embedded && <h1>Download preferences</h1>}

      {admin && (
        <label className="settings-scope-picker">
          Apply to
          <select
            value={scope}
            onChange={(event) => {
              setSaved(false);
              setScope(event.target.value as Scope);
            }}
          >
            <option value="personal">Only me</option>
            <option value="installation">Everyone on this server</option>
          </select>
        </label>
      )}
      <Notice error={current.error} />
      {saved && (
        <p className="success" role="status">
          {librariesOnly
            ? "Library defaults saved."
            : "Download defaults saved."}
        </p>
      )}
      {current.isPending && <Loading />}
      {current.data && (
        <Editor
          key={`${scope}:${current.data.revision}`}
          admin={admin}
          scope={scope}
          librariesOnly={librariesOnly}
          current={current.data}
          onSaved={() => setSaved(true)}
          onEdit={() => setSaved(false)}
        />
      )}
    </section>
  );
}

function Editor({
  admin,
  scope,
  librariesOnly,
  current,
  onSaved,
  onEdit,
}: {
  admin: boolean;
  scope: Scope;
  librariesOnly: boolean;
  current: Defaults;
  onSaved: () => void;
  onEdit: () => void;
}) {
  const cache = useQueryClient();
  const [overrides, setOverrides] = useState(current.overrides);
  const [merge, setMerge] = useState<boolean | null>(null);
  const showAudiobookImport =
    admin && !librariesOnly && scope === "installation";
  const organization = useQuery({
    queryKey: ["organization-settings"],
    enabled: showAudiobookImport,
    queryFn: async () => result(await api.GET("/api/organization/settings")),
  });
  const profile = organization.data?.profile;
  const mergeFiles = merge ?? profile?.merge_mp3_chapters ?? false;
  const mergeChanged =
    profile != null && mergeFiles !== profile.merge_mp3_chapters;
  const preferencesChanged =
    JSON.stringify(overrides) !== JSON.stringify(current.overrides);
  const save = useMutation({
    mutationFn: async () => {
      const preferences = preferencesChanged
        ? await result(
            await api.PUT("/api/acquisition/preferences/{scope}", {
              params: { path: { scope } },
              body: { overrides, expected_revision: current.revision },
            }),
          )
        : current;
      const settings =
        mergeChanged && profile && organization.data
          ? await result(
              await api.PUT("/api/organization/settings", {
                body: {
                  profile: { ...profile, merge_mp3_chapters: mergeFiles },
                  expected_revision: organization.data.revision,
                },
              }),
            )
          : null;
      return { preferences, settings };
    },
    onSuccess: async ({ preferences, settings }) => {
      onSaved();
      setMerge(null);
      cache.setQueryData(["download-defaults", scope], preferences);
      if (settings) cache.setQueryData(["organization-settings"], settings);
      await Promise.all([
        cache.invalidateQueries({ queryKey: ["release-profiles"] }),
        cache.invalidateQueries({ queryKey: ["download-defaults"] }),
        cache.invalidateQueries({ queryKey: ["organization"] }),
      ]);
    },
  });
  return (
    <form
      className="panel editor"
      aria-label="Download default settings"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      {showAudiobookImport && (
        <section className="audiobook-download-option">
          <h3>Audiobook downloads</h3>
          <label className="check-label">
            <input
              type="checkbox"
              checked={mergeFiles}
              disabled={save.isPending || !profile}
              onChange={(event) => {
                onEdit();
                setMerge(event.target.checked);
              }}
            />
            Merge MP3 chapters into one M4B
            <SettingHelp label="chapter merging">
              A finished download of several MP3 files becomes one M4B before it
              enters the library. Each file becomes a chapter. The download
              stays unchanged for seeding.
            </SettingHelp>
          </label>
          <p>Applies to every audiobook download on this server.</p>
          <Notice error={organization.error} />
        </section>
      )}
      {librariesOnly ? (
        <>
          <ScopeFields
            librariesOnly
            defaults
            overrides={overrides}
            inherited={current.inherited}
            origins={current.inherited_origins}
            onChange={(value) => {
              onEdit();
              setOverrides(value);
            }}
          />
          <RouteFields
            overrides={overrides}
            inherited={current.inherited}
            origins={current.inherited_origins}
            onChange={(value) => {
              onEdit();
              setOverrides(value);
            }}
          />
        </>
      ) : (
        <PreferenceFields
          defaults
          overrides={overrides}
          inherited={current.inherited}
          origins={current.inherited_origins}
          onChange={(value) => {
            onEdit();
            setOverrides(value);
          }}
        />
      )}
      {!librariesOnly && (
        <Link className="settings-inline-link" to="/settings#libraries">
          Library folders & download routes →
        </Link>
      )}
      <Notice error={save.error} />
      <div className="button-row">
        <button
          className="primary"
          disabled={save.isPending || (!preferencesChanged && !mergeChanged)}
        >
          {librariesOnly ? "Save library defaults" : "Save download defaults"}
        </button>
        <button
          type="button"
          onClick={() => {
            onEdit();
            const libraryKeys = new Set([
              "audio_library_id",
              "ebook_library_id",
              "downloader_id",
              "torrent_downloader_id",
              "usenet_downloader_id",
              "ebook_destination_id",
              "audio_destination_id",
            ]);
            setOverrides(
              Object.fromEntries(
                Object.entries(overrides).filter(([key]) =>
                  librariesOnly ? !libraryKeys.has(key) : libraryKeys.has(key),
                ),
              ),
            );
          }}
        >
          Use inherited defaults
        </button>
      </div>
    </form>
  );
}
