import SettingHelp from "../components/SettingHelp";
import Order from "./PreferenceOrder";
import SourcePriorities from "./SourcePriorities";
import ScopeFields from "./ScopeFields";
import RouteFields, { EffectiveRoutes } from "./RouteFields";
import NarratorNamesField from "./NarratorNamesField";
import type { components } from "../api/schema";
type Preferences = components["schemas"]["ReleasePreferences"];
export type Overrides = components["schemas"]["PreferenceOverrides"];
export const preferenceLabels: Partial<Record<keyof Preferences, string>> = {
  allow_unknown_seeders: "Allow unknown seed counts",
  criteria: "Ranking priorities",
  source_order: "Source preference",
  source_strategy: "Quick add source strategy",
  source_fallback: "Fall back to the next source",
  ebook_formats: "Ebook format preference",
  audio_formats: "Audiobook format preference",
  blocked_formats: "Blocked formats",
  maximum_bytes: "Maximum transfer size",
  preferred_narrators: "Preferred narrators",
  search_series: "Search known series names",
  prefer_series_packs: "Prefer eligible series packs",
  series_scope: "Series scope",
  recording_style: "Audiobook recordings",
};
const recordingStyleLabels = {
  any: "Narrated or dramatized",
  narrated: "Narrated only",
  dramatized: "Dramatized adaptations only",
};
export const seriesScopeLabels = {
  just_book: "Just this book",
  prefer_packs: "Prefer series packs",
  complete_series: "Complete reviewed series",
};
export function effectiveSeriesScope(
  preferences: Pick<Preferences, "series_scope" | "prefer_series_packs">,
) {
  return (
    preferences.series_scope ||
    (preferences.prefer_series_packs ? "prefer_packs" : "just_book")
  );
}
const formats = [
  "epub",
  "pdf",
  "mobi",
  "azw",
  "azw3",
  "cbz",
  "cbr",
  "m4b",
  "mp3",
  "flac",
  "aac",
  "ogg",
  "opus",
];

export default function PreferenceFields({
  overrides,
  inherited,
  origins,
  onChange,
  includeMedia = true,
  defaults = false,
}: {
  overrides: Overrides;
  inherited: Preferences;
  origins: Record<string, string>;
  onChange: (value: Overrides) => void;
  includeMedia?: boolean;
  defaults?: boolean;
}) {
  const effective = { ...inherited, ...overrides };
  if (
    Object.hasOwn(overrides, "prefer_series_packs") &&
    !Object.hasOwn(overrides, "series_scope")
  )
    delete effective.series_scope;
  const origin = (key: keyof Preferences) =>
    Object.hasOwn(overrides, key) ? (
      <div className="preference-origin">
        <SettingHelp label="inherited value">
          {Object.hasOwn(overrides, key)
            ? "Custom value"
            : `Inherited · ${origins[key] || "default"}`}
        </SettingHelp>
        {Object.hasOwn(overrides, key) && (
          <button
            type="button"
            aria-label={`Use inherited ${preferenceLabels[key]}`}
            onClick={() => {
              const next = { ...overrides };
              delete next[key];
              onChange(next);
            }}
          >
            Reset
          </button>
        )}
      </div>
    ) : null;
  const order = (
    key: "criteria" | "source_order" | "ebook_formats" | "audio_formats",
  ) => (
    <div>
      <Order
        label={preferenceLabels[key]!}
        values={effective[key] || []}
        valid={(values) =>
          key !== "criteria" ||
          !values.includes("popularity") ||
          values.indexOf("source") < values.indexOf("popularity")
        }
        onChange={(values) => onChange({ ...overrides, [key]: values })}
      />
      {origin(key)}
    </div>
  );
  return (
    <>
      <ScopeFields
        overrides={overrides}
        inherited={inherited}
        origins={origins}
        onChange={onChange}
        includeMedia={includeMedia}
        defaults={defaults}
      />
      <div className="preference-ranking-grid">
        <section className="priority-block">
          {order("criteria")}{" "}
          <div className="setting-inline-option">
            <label className="check-label">
              <input
                type="checkbox"
                checked={effective.allow_unknown_seeders ?? false}
                onChange={(event) =>
                  onChange({
                    ...overrides,
                    allow_unknown_seeders: event.target.checked,
                  })
                }
              />
              Allow AudiobookBay releases after torrent metadata resolves
            </label>
            <div className="setting-help-row">
              <SettingHelp label="download preferences">
                Off by default. Metadata resolution verifies the torrent
                manifest, not a seeder count or guaranteed payload availability.
                All identity, format, size and import checks still apply.
              </SettingHelp>
            </div>
            {origin("allow_unknown_seeders")}
          </div>
          <div className="setting-inline-option">
            <label className="check-label">
              <input
                type="checkbox"
                checked={effective.criteria?.includes("popularity") || false}
                onChange={(event) => {
                  const criteria: Preferences["criteria"] = (
                    effective.criteria || ["format", "source", "seeders"]
                  ).filter((value) => value !== "popularity");
                  if (event.target.checked)
                    criteria.splice(
                      criteria.indexOf("source") + 1,
                      0,
                      "popularity",
                    );
                  onChange({ ...overrides, criteria });
                }}
              />
              Prefer popular releases
            </label>
            <SettingHelp label="source popularity">
              Ranks popularity within each source, after source priority.
            </SettingHelp>
          </div>
        </section>
        <section className="priority-block">
          <label>
            Quick add
            <select
              aria-label="Quick add source strategy"
              value={effective.source_strategy ?? "priority"}
              onChange={(event) =>
                onChange({
                  ...overrides,
                  source_strategy: event.target.value as
                    "priority" | "rank_all",
                })
              }
            >
              <option value="priority">Prefer the first source</option>
              <option value="rank_all">Rank every connected source</option>
            </select>
          </label>
          <label className="check-label">
            <input
              type="checkbox"
              checked={effective.source_fallback ?? true}
              disabled={
                (effective.source_strategy ?? "priority") !== "priority"
              }
              onChange={(event) =>
                onChange({
                  ...overrides,
                  source_fallback: event.target.checked,
                })
              }
            />
            If that source has no automatic match, try the next source
          </label>
          <SourcePriorities
            values={effective.source_order || []}
            onChange={(source_order) =>
              onChange({ ...overrides, source_order })
            }
          />
          {origin("source_order")}
        </section>
      </div>
      <RouteFields
        overrides={overrides}
        inherited={inherited}
        origins={origins}
        onChange={onChange}
      />
      <details>
        <summary>
          <span className="setting-subheading">
            Series search
            <SettingHelp label="download preferences">
              Packs are limited to 20 additional books and 50 GiB, or your lower
              size limit. Complete series uses a reviewed book list.
            </SettingHelp>
          </span>
        </summary>
        <label className="check-label">
          <input
            type="checkbox"
            checked={effective.search_series ?? true}
            onChange={(event) =>
              onChange({ ...overrides, search_series: event.target.checked })
            }
          />
          Search known series names alongside the title
        </label>
        {origin("search_series")}
        <label>
          Series scope
          <select
            value={effectiveSeriesScope(effective)}
            onChange={(event) => {
              const next = {
                ...overrides,
                series_scope: event.target.value as NonNullable<
                  Overrides["series_scope"]
                >,
              };
              delete next.prefer_series_packs;
              onChange(next);
            }}
          >
            {Object.entries(seriesScopeLabels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        {origin(
          effective.series_scope ? "series_scope" : "prefer_series_packs",
        )}
      </details>
      {!defaults && (
        <details>
          <summary>Narrator preferences</summary>
          <NarratorNamesField
            label="Preferred narrators"
            ordered
            values={effective.preferred_narrators || []}
            onChange={(preferred_narrators) =>
              onChange({ ...overrides, preferred_narrators })
            }
          />
          {origin("preferred_narrators")}
          {!effective.criteria?.includes("narrator") ? (
            <>
              <div className="setting-help-row">
                <SettingHelp label="download preferences">
                  Narrator preference breaks ties after the ranking priorities
                  above.
                </SettingHelp>
              </div>
              <button
                type="button"
                onClick={() =>
                  onChange({
                    ...overrides,
                    criteria: [
                      "narrator",
                      ...(effective.criteria || [
                        "format",
                        "source",
                        "seeders",
                      ]),
                    ],
                  })
                }
              >
                Rank narrator preference first
              </button>
            </>
          ) : (
            <>
              <div className="setting-help-row">
                <SettingHelp label="download preferences">
                  Move narrator in Ranking priorities to choose when this
                  preference applies.
                </SettingHelp>
              </div>
              <button
                type="button"
                onClick={() =>
                  onChange({
                    ...overrides,
                    criteria: effective.criteria?.filter(
                      (criterion) => criterion !== "narrator",
                    ),
                  })
                }
              >
                Use narrator preference only to break ties
              </button>
            </>
          )}
        </details>
      )}
      <details>
        <summary>
          <span className="setting-subheading">
            Formats and transfer limits
            <SettingHelp label="download preferences">
              A blank custom limit means no custom size limit. Independent
              request restrictions and installation capacity limits still apply.
              Blocked formats apply to the whole transfer.
            </SettingHelp>
          </span>
        </summary>
        {order("ebook_formats")}
        {order("audio_formats")}
        <label>
          <span className="setting-subheading">
            Audiobook recordings
            <SettingHelp label="download preferences">
              A dramatized adaptation, such as a GraphicAudio or full-cast
              recording, is an audio edition of the same book and counts as
              owning it.
            </SettingHelp>
          </span>
          <select
            value={effective.recording_style ?? "any"}
            onChange={(event) =>
              onChange({
                ...overrides,
                recording_style: event.target
                  .value as keyof typeof recordingStyleLabels,
              })
            }
          >
            {Object.entries(recordingStyleLabels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        {origin("recording_style")}
        <fieldset className="format-checkbox-grid">
          <legend>Blocked formats</legend>
          {formats.map((format) => (
            <label className="check-label" key={format}>
              <input
                type="checkbox"
                checked={effective.blocked_formats?.includes(format) || false}
                onChange={(event) =>
                  onChange({
                    ...overrides,
                    blocked_formats: event.target.checked
                      ? [...(effective.blocked_formats || []), format]
                      : (effective.blocked_formats || []).filter(
                          (f) => f !== format,
                        ),
                  })
                }
              />
              {format.toUpperCase()}
            </label>
          ))}
        </fieldset>
        {origin("blocked_formats")}
        <label>
          Maximum transfer size (GiB, optional)
          <input
            type="number"
            min="0.01"
            max={Number.MAX_SAFE_INTEGER / 1024 ** 3}
            step="any"
            value={
              effective.maximum_bytes == null
                ? ""
                : effective.maximum_bytes / 1024 ** 3
            }
            onChange={(event) => {
              const bytes =
                event.target.value === ""
                  ? null
                  : Math.round(Number(event.target.value) * 1024 ** 3);
              if (bytes === null || (Number.isSafeInteger(bytes) && bytes > 0))
                onChange({ ...overrides, maximum_bytes: bytes });
            }}
          />
        </label>
        {origin("maximum_bytes")}
      </details>
    </>
  );
}

export function EffectivePreferences({
  preferences,
  origins,
}: {
  preferences: Preferences;
  origins: Record<string, string>;
}) {
  return (
    <details>
      <summary>Effective download preferences</summary>
      <EffectiveRoutes preferences={preferences} origins={origins} />
      <dl>
        {(Object.keys(preferenceLabels) as (keyof Preferences)[])
          .filter((key) => key !== "prefer_series_packs")
          .map((key) => (
            <div key={key}>
              <dt>{preferenceLabels[key]}</dt>
              <dd>
                {key === "series_scope"
                  ? seriesScopeLabels[effectiveSeriesScope(preferences)]
                  : key === "recording_style"
                    ? recordingStyleLabels[preferences.recording_style ?? "any"]
                    : Array.isArray(preferences[key])
                      ? (preferences[key] as string[]).join(" → ") || "None"
                      : typeof preferences[key] === "boolean"
                        ? preferences[key]
                          ? "Yes"
                          : "No"
                        : preferences[key] == null
                          ? "No custom limit"
                          : `${preferences[key]} bytes`}
                <small>
                  {" "}
                  ·{" "}
                  {origins[key] ||
                    (key === "series_scope" && origins.prefer_series_packs) ||
                    "Saved preferences"}
                </small>
              </dd>
            </div>
          ))}
      </dl>
    </details>
  );
}
