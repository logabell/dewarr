import LanguageSelect from "../components/LanguageSelect";
import SettingHelp from "../components/SettingHelp";
import { useQuery } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Notice } from "../components";
import NarratorNamesField from "./NarratorNamesField";
type Preferences = components["schemas"]["ReleasePreferences"];
type Overrides = components["schemas"]["PreferenceOverrides"];
type Spec = components["schemas"]["RequestSpec"];
export const scopeLabels = {
  desired_media: "Default requested media",
  preferred_medium: "Default first medium for Either",
  language: "Required language",
  abridged: "Audiobook abridgment",
  required_narrators: "Required narrators",
  standalone: "Standalone copies",
  ebook_library_id: "Ebook library",
  audio_library_id: "Audiobook library",
} as const;
const media = {
  ebook: "Ebook",
  audio: "Audiobook",
  both: "Both",
  either: "Either",
};
function useLibraries() {
  return useQuery({
    queryKey: ["libraries"],
    queryFn: async () => result(await api.GET("/api/library/libraries")),
  });
}
export default function ScopeFields({
  overrides,
  inherited,
  origins,
  onChange,
  includeMedia = true,
  librariesOnly = false,
  defaults = false,
}: {
  overrides: Overrides;
  inherited: Preferences;
  origins: Record<string, string>;
  onChange: (value: Overrides) => void;
  includeMedia?: boolean;
  librariesOnly?: boolean;
  defaults?: boolean;
}) {
  const libraries = useLibraries();
  const values = { ...inherited, ...overrides };
  const showLibraries = !defaults || librariesOnly;
  const availableLibraries = libraries.data?.filter(
    (library) => library.accessible,
  );
  const unavailableDefault = [
    values.ebook_library_id,
    values.audio_library_id,
  ].some(
    (id) => id && !availableLibraries?.some((library) => library.id === id),
  );
  const origin = (field: keyof typeof scopeLabels) =>
    Object.hasOwn(overrides, field) ? (
      <div className="preference-origin">
        <SettingHelp label="inherited value">
          {Object.hasOwn(overrides, field)
            ? "Custom value"
            : `Inherited · ${origins[field] || "default"}`}
        </SettingHelp>
        {Object.hasOwn(overrides, field) && (
          <button
            type="button"
            aria-label={`Use inherited ${scopeLabels[field]}`}
            onClick={() => {
              const next = { ...overrides };
              delete next[field];
              onChange(next);
            }}
          >
            Reset
          </button>
        )}
      </div>
    ) : null;
  return (
    <section className="scope-fields">
      <div className="setting-subheading">
        <h3>{librariesOnly ? "Default libraries" : "Media & language"}</h3>
        <SettingHelp label="download preferences">
          Used when a request leaves the corresponding choice inherited.
          Existing requests keep their accepted scope. Library choices still
          require current access and a verified import route.
        </SettingHelp>
      </div>
      <Notice error={libraries.error} />
      {showLibraries &&
        libraries.isSuccess &&
        (!availableLibraries?.length ? (
          <p className="notice" role="status">
            No libraries are available to your account. An administrator can
            check the connection and grant library access in Settings →
            Libraries.
          </p>
        ) : unavailableDefault ? (
          <p className="notice" role="status">
            A saved default library is unavailable to your account. Choose an
            available library or ask an administrator to check your library
            access in Settings → Libraries.
          </p>
        ) : null)}
      {!librariesOnly && (
        <>
          {includeMedia && (
            <>
              <label>
                {scopeLabels.desired_media}
                <select
                  aria-label={scopeLabels.desired_media}
                  value={values.desired_media || ""}
                  onChange={(e) =>
                    onChange({
                      ...overrides,
                      desired_media:
                        (e.target.value as Preferences["desired_media"]) ||
                        null,
                    })
                  }
                >
                  <option value="">Choose on each request</option>
                  {Object.entries(media).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              {origin("desired_media")}
              {values.desired_media === "either" && (
                <>
                  <label>
                    Search first
                    <select
                      aria-label="Search first"
                      value={values.preferred_medium}
                      onChange={(e) =>
                        onChange({
                          ...overrides,
                          preferred_medium: e.target.value as "ebook" | "audio",
                        })
                      }
                    >
                      <option value="audio">Audiobook</option>
                      <option value="ebook">Ebook</option>
                    </select>
                  </label>
                  {origin("preferred_medium")}
                </>
              )}
            </>
          )}
          <label>
            {scopeLabels.language}
            <LanguageSelect
              allowAny
              value={values.language || ""}
              onChange={(language) =>
                onChange({ ...overrides, language: language || null })
              }
            />
          </label>
          {origin("language")}
          <label>
            {scopeLabels.abridged}
            <select
              value={values.abridged == null ? "" : String(values.abridged)}
              onChange={(e) =>
                onChange({
                  ...overrides,
                  abridged:
                    e.target.value === "" ? null : e.target.value === "true",
                })
              }
            >
              <option value="">Any abridgment</option>
              <option value="false">Unabridged only</option>
              <option value="true">Abridged only</option>
            </select>
          </label>
          {origin("abridged")}
          {!defaults && (
            <>
              <NarratorNamesField
                label={scopeLabels.required_narrators}
                values={values.required_narrators || []}
                onChange={(required_narrators) =>
                  onChange({ ...overrides, required_narrators })
                }
              />
              {origin("required_narrators")}
            </>
          )}
          <label className="check-label">
            <input
              type="checkbox"
              checked={values.standalone || false}
              onChange={(e) =>
                onChange({ ...overrides, standalone: e.target.checked })
              }
            />
            Require standalone copies
          </label>
          {origin("standalone")}
        </>
      )}
      {showLibraries &&
        (["ebook_library_id", "audio_library_id"] as const).map((field) => (
          <div key={field}>
            <label>
              {scopeLabels[field]}
              <select
                aria-label={scopeLabels[field]}
                value={values[field] || ""}
                onChange={(e) =>
                  onChange({ ...overrides, [field]: e.target.value || null })
                }
              >
                <option value="">Choose during acquisition</option>
                {values[field] &&
                  !availableLibraries?.some((l) => l.id === values[field]) && (
                    <option value={values[field]!}>
                      Unavailable saved library
                    </option>
                  )}
                {libraries.data
                  ?.filter((l) => l.accessible)
                  .map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.name}
                    </option>
                  ))}
              </select>
            </label>
            {origin(field)}
          </div>
        ))}
    </section>
  );
}
export function EffectiveScope({
  specification,
  origins = {},
}: {
  specification: Spec;
  origins?: Record<string, string>;
}) {
  const libraries = useLibraries();
  const library = (id: string | null | undefined) =>
    id
      ? libraries.data?.find((l) => l.id === id)?.name ||
        "Unavailable saved library"
      : "Choose during acquisition";
  const rows = [
    ["mode", "Requested media", media[specification.mode]],
    ...(specification.mode === "either"
      ? [
          [
            "preferred_medium",
            "Search first",
            media[specification.preferred_medium || "audio"],
          ],
        ]
      : []),
    ["language", "Required language", specification.language || "Any language"],
    [
      "standalone",
      "Standalone copies",
      specification.standalone ? "Required" : "Omnibus allowed",
    ],
    ...(specification.mode !== "audio"
      ? [
          [
            "ebook_library_id",
            "Ebook library",
            library(specification.ebook_library_id),
          ],
        ]
      : []),
    ...(specification.mode !== "ebook"
      ? [
          [
            "audio_library_id",
            "Audiobook library",
            library(specification.audio_library_id),
          ],
          [
            "abridged",
            "Audiobook abridgment",
            specification.abridged == null
              ? "Any"
              : specification.abridged
                ? "Abridged"
                : "Unabridged",
          ],
          [
            "required_narrators",
            "Required narrators",
            specification.required_narrators?.join("; ") || "Any narrator",
          ],
        ]
      : []),
  ];
  return (
    <details>
      <summary>Effective request scope</summary>
      <dl>
        {rows.map(([key, label, value]) => (
          <div key={key}>
            <dt>{label}</dt>
            <dd>
              {value}
              <small> · {origins[key] || "Saved request"}</small>
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}
