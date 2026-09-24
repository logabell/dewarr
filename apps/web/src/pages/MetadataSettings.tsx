import ConnectionStatus from "../components/ConnectionStatus";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import LanguageSelect from "../components/LanguageSelect";
import SettingHelp from "../components/SettingHelp";

type Preferences = components["schemas"]["MetadataPreferences"];
const fields = [
  "title",
  "authors",
  "description",
  "publication_year",
  "language",
];
export const fieldLabel = (field: string) =>
  ({ publication_year: "Publication year", cover_url: "Cover" })[field] ||
  field.charAt(0).toUpperCase() + field.slice(1);

/** Minimum Hardcover personal-access-token scopes for full Dewarr use. */
const hardcoverScopes = [
  [
    "read:catalog",
    "Search, plus book, author, series, and edition details. Required to test the connection.",
  ],
  [
    "read:me:content",
    "Your user id, so your lists stay separate from lists you follow.",
  ],
  ["read:lists", "Your lists, lists you follow, and private lists."],
  ["read:library:public", "Public reviews on book pages."],
  ["read:users", "Usernames on those reviews."],
  ["write:lists", "Add and remove books on lists you own."],
] as const;
const hardcoverTokenUrl = `https://hardcover.app/account/api/keys/new?scope=${hardcoverScopes
  .map(([scope]) => scope)
  .join("+")}`;

export default function MetadataSettings({
  admin,
  embedded = false,
}: {
  admin: boolean;
  embedded?: boolean;
}) {
  const client = useQueryClient();
  const [token, setToken] = useState("");
  const [message, setMessage] = useState("");
  const account = useQuery({
    queryKey: ["metadata-account"],
    queryFn: async () => result(await api.GET("/api/metadata/account")),
  });
  const preferences = useQuery({
    queryKey: ["metadata-preferences"],
    queryFn: async () => result(await api.GET("/api/metadata/preferences")),
  });
  const save = useMutation({
    mutationFn: async (enabled: boolean) =>
      result(
        await api.PUT("/api/metadata/account", {
          body: { token: token || null, enabled },
        }),
      ),
    onSuccess: (value) => {
      client.setQueryData(["metadata-account"], value);
      client.removeQueries({ queryKey: ["discovery"] });
      client.resetQueries({ queryKey: ["reading-hardcover-lists"] });
      setToken("");
      setMessage("Your catalog connection was saved.");
    },
  });
  const suggestions = useMutation({
    mutationFn: async (enabled: boolean) =>
      result(
        await api.PUT("/api/metadata/account/series-suggestions", {
          body: { enabled },
        }),
      ),
    onSuccess: (value) => {
      client.setQueryData(["metadata-account"], value);
      void client.invalidateQueries({ queryKey: ["discovery", "series"] });
      void client.invalidateQueries({ queryKey: ["series-gaps"] });
      setMessage(
        value.suggest_series_gaps
          ? "Dewarr will look for missing books in series linked to your library."
          : "Series suggestions are off.",
      );
    },
  });
  const test = useMutation({
    mutationFn: async () =>
      result(await api.POST("/api/metadata/account/test")),
    onSuccess: (value) => {
      client.setQueryData(["metadata-account"], value);
      if (value.status === "connected") {
        void client.invalidateQueries({
          queryKey: ["reading-hardcover-lists"],
        });
      }
      setMessage(
        value.status === "connected"
          ? "Hardcover catalog access verified."
          : "",
      );
    },
  });
  return (
    <>
      {!embedded && (
        <div className="page-heading">
          <div>
            <p className="eyebrow">YOUR CATALOG</p>
            <h1>Metadata settings</h1>
            <p className="muted">
              Useful defaults, with control where you need it.
            </p>
          </div>
        </div>
      )}
      <section className="settings-block metadata-connection">
        <div className="setting-subheading">
          <h3>Hardcover</h3>
          <SettingHelp label="Hardcover">
            <span className="help-heading">Minimum API permissions</span>
            <span className="help-scope-list">
              {hardcoverScopes.map(([scope]) => (
                <code key={scope}>{scope}</code>
              ))}
            </span>
          </SettingHelp>
          {account.data && (
            <ConnectionStatus
              status={test.isPending ? "checking" : account.data.status}
            />
          )}
        </div>
        <Notice
          error={account.error || save.error || test.error || suggestions.error}
        />
        {account.isPending && <Loading />}
        {account.data && (
          <>
            {account.data.last_error && (
              <p className="notice error">{account.data.last_error}</p>
            )}
            <form
              className="metadata-form"
              onSubmit={(e) => {
                e.preventDefault();
                save.mutate(true);
              }}
            >
              <label>
                Hardcover API token
                <input
                  type="password"
                  autoComplete="off"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  required={!account.data.configured}
                  maxLength={8192}
                  placeholder={
                    account.data.configured ? "••••••••" : "Enter your token"
                  }
                />
              </label>
              <div className="button-row metadata-actions">
                <button
                  className="primary"
                  disabled={save.isPending || test.isPending}
                >
                  Save connection
                </button>
                {account.data.configured && (
                  <>
                    <button
                      type="button"
                      onClick={() => test.mutate()}
                      disabled={
                        !account.data.enabled ||
                        test.isPending ||
                        save.isPending ||
                        !!token
                      }
                    >
                      {test.isPending ? "Testing…" : "Test connection"}
                    </button>
                    <button
                      type="button"
                      onClick={() => save.mutate(!account.data!.enabled)}
                      disabled={save.isPending || test.isPending}
                    >
                      {account.data.enabled
                        ? "Disable connection"
                        : "Enable connection"}
                    </button>
                  </>
                )}
                <a
                  className="metadata-token-link"
                  href={hardcoverTokenUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  Create a token ↗
                </a>
              </div>
            </form>
            {account.data.configured && account.data.enabled && (
              <div className="metadata-option">
                <label className="check-label">
                  <input
                    type="checkbox"
                    checked={account.data.suggest_series_gaps}
                    disabled={suggestions.isPending}
                    onChange={(event) =>
                      suggestions.mutate(event.target.checked)
                    }
                  />
                  Suggest missing books in series I own
                </label>
                <p className="muted">
                  Uses Hardcover links on books you already matched. Dewarr
                  loads those series catalogs and lists published books that are
                  not in your library. Nothing is downloaded.
                </p>
              </div>
            )}
          </>
        )}
        {message && (
          <p className="success" role="status">
            {message}
          </p>
        )}
      </section>
      <Notice error={preferences.error} />
      {preferences.data &&
        (admin ? (
          <PreferenceForm value={preferences.data} />
        ) : (
          <p className="muted">
            Catalog defaults are managed by your administrator. Your token and
            lists remain private.
          </p>
        ))}
    </>
  );
}

function PreferenceForm({ value }: { value: Preferences }) {
  const client = useQueryClient();
  const [settings, setSettings] = useState(value);
  const [saved, setSaved] = useState(false);
  const save = useMutation({
    mutationFn: async () =>
      result(await api.PUT("/api/metadata/preferences", { body: settings })),
    onSuccess: (result) => {
      client.setQueryData(["metadata-preferences"], result);
      setSaved(true);
    },
  });
  return (
    <form
      className="settings-block editor metadata-defaults"
      onChange={() => setSaved(false)}
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <h3>Catalog defaults</h3>
      <div className="settings-fields">
        <label>
          Primary catalog
          <select
            value={settings.primary}
            onChange={(e) => {
              setSaved(false);
              setSettings({
                ...settings,
                primary: e.target.value as Preferences["primary"],
              });
            }}
          >
            <option value="hardcover">Hardcover</option>
            <option value="openlibrary">Open Library</option>
          </select>
        </label>
        <label>
          Primary language
          <LanguageSelect
            value={settings.language}
            onChange={(language) => {
              setSaved(false);
              setSettings({ ...settings, language });
            }}
          />
        </label>
      </div>
      <label className="check-label">
        <input
          type="checkbox"
          checked={settings.filter_language ?? false}
          onChange={(event) => {
            setSaved(false);
            setSettings({ ...settings, filter_language: event.target.checked });
          }}
        />
        Only search books available in this language
      </label>
      <details className="settings-disclosure metadata-advanced">
        <summary>Advanced metadata</summary>
        <div className="metadata-automation">
          <label className="check-label">
            <input
              type="checkbox"
              checked={settings.automatic_enrichment ?? true}
              onChange={(event) =>
                setSettings({
                  ...settings,
                  automatic_enrichment: event.target.checked,
                })
              }
            />
            Fill missing details from Open Library
          </label>
          <label className="check-label">
            <input
              type="checkbox"
              checked={settings.automatic_edition_lookup ?? true}
              onChange={(event) =>
                setSettings({
                  ...settings,
                  automatic_edition_lookup: event.target.checked,
                })
              }
            />
            Look up missing editions before import
          </label>
          <label className="check-label">
            <input
              type="checkbox"
              checked={settings.automatic_library_matching ?? true}
              onChange={(event) =>
                setSettings({
                  ...settings,
                  automatic_library_matching: event.target.checked,
                })
              }
            />
            Match library books automatically
            <SettingHelp label="automatic library matching">
              After each library sync, Dewarr asks Hardcover about up to 25
              unmatched library books using your Hardcover connection. Only a
              single verified match is saved; anything else stays in library
              review with its candidates.
            </SettingHelp>
          </label>
          <label className="check-label">
            <input
              type="checkbox"
              checked={settings.write_library_series ?? false}
              disabled={!(settings.automatic_library_matching ?? true)}
              onChange={(event) =>
                setSettings({
                  ...settings,
                  write_library_series: event.target.checked,
                })
              }
            />
            Add the series to matched Audiobookshelf books
            <SettingHelp label="series write-back">
              When automatic matching links a book, Dewarr sets its Hardcover
              series and position on Audiobookshelf items that have no series.
              Items that already have a series, and every other field, are left
              alone. Audio files are never changed.
            </SettingHelp>
          </label>
          <label className="check-label">
            <input
              type="checkbox"
              checked={settings.combine_library_parts ?? true}
              onChange={(event) =>
                setSettings({
                  ...settings,
                  combine_library_parts: event.target.checked,
                })
              }
            />
            Combine complete multi-part books into one Audiobookshelf book
            <SettingHelp label="combining parts">
              When every part of a book released in parts is in an
              Audiobookshelf library, Dewarr moves them into one book folder
              with a Disc folder per part, keeping each part's files. The old
              part folders are removed only after Audiobookshelf shows the
              combined book, and you can separate them again from the book page.
              Books whose parts your connected Audiobookshelf account has
              started are skipped, so listening progress is kept. This needs the
              library set up as an audio import destination.
            </SettingHelp>
          </label>
        </div>
        <div className="settings-fields">
          <label>
            Cover provider
            <select
              value={settings.field_providers?.cover_url || settings.covers}
              onChange={(e) => {
                const next = { ...settings.field_providers };
                delete next.cover_url;
                setSettings({
                  ...settings,
                  covers: e.target.value as Preferences["covers"],
                  field_providers: next,
                });
              }}
            >
              <option value="automatic">Use primary catalog</option>
              <option value="hardcover">Hardcover</option>
              <option value="openlibrary">Open Library</option>
            </select>
          </label>
        </div>
        <div className="setting-subheading">
          <h3>Field overrides</h3>
          <SettingHelp label="field overrides">
            Override the primary catalog for individual details. Fields without
            an override use the primary catalog.
          </SettingHelp>
        </div>
        <div className="settings-fields metadata-provider-grid">
          {fields.map((field) => (
            <label key={field}>
              {fieldLabel(field)}
              <select
                value={settings.field_providers?.[field] || ""}
                onChange={(e) => {
                  const next = { ...settings.field_providers };
                  if (e.target.value)
                    next[field] = e.target.value as "hardcover" | "openlibrary";
                  else delete next[field];
                  setSettings({ ...settings, field_providers: next });
                }}
              >
                <option value="">Primary catalog</option>
                <option value="hardcover">Hardcover</option>
                <option value="openlibrary">Open Library</option>
              </select>
            </label>
          ))}
        </div>
        <button
          type="button"
          onClick={() =>
            setSettings({
              ...settings,
              automatic_enrichment: true,
              automatic_edition_lookup: true,
              automatic_library_matching: true,
              write_library_series: false,
              combine_library_parts: true,
              covers: "automatic",
              field_providers: {},
            })
          }
        >
          Reset advanced preferences
        </button>
      </details>
      <Notice error={save.error} />
      <div className="button-row metadata-actions">
        <button className="primary" disabled={save.isPending}>
          Save metadata defaults
        </button>
      </div>
      {saved && (
        <p className="success" role="status">
          Metadata defaults saved.
        </p>
      )}
    </form>
  );
}
