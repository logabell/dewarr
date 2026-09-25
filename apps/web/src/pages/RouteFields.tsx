import SettingHelp from "../components/SettingHelp";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Notice } from "../components";

type Preferences = components["schemas"]["ReleasePreferences"];
type Overrides = components["schemas"]["PreferenceOverrides"];
export const routeLabels = {
  torrent_downloader_id: "Default torrent downloader",
  usenet_downloader_id: "Default Usenet downloader",
  ebook_destination_id: "Default ebook destination",
  audio_destination_id: "Default audiobook destination",
} as const;
type Field = keyof typeof routeLabels;
const downloaderFields = [
  { field: "torrent_downloader_id", protocol: "torrent" },
  { field: "usenet_downloader_id", protocol: "nzb" },
] as const;
type DestinationChoice = { id: string; library_id: string; medium: string };

type DownloaderChoice = { id: string; name: string; protocol?: string };

function protocolFor(field: Field) {
  if (field === "torrent_downloader_id") return "torrent";
  if (field === "usenet_downloader_id") return "nzb";
  return null;
}

export function protocolPreference(
  preferences: Partial<Preferences> | undefined,
  protocol: "torrent" | "nzb",
  downloaders: DownloaderChoice[] = [],
) {
  const specific =
    protocol === "torrent"
      ? preferences?.torrent_downloader_id
      : preferences?.usenet_downloader_id;
  if (specific) return specific;
  const legacy = preferences?.downloader_id;
  const client = downloaders.find((item) => item.id === legacy);
  if (client?.protocol === protocol) return legacy;
  const matching = downloaders.filter((item) => item.protocol === protocol);
  return matching.length === 1 ? matching[0].id : undefined;
}

export function primaryDownloaderPreference(
  preferences: Partial<Preferences> | undefined,
  downloaders: DownloaderChoice[] = [],
) {
  return (
    preferences?.torrent_downloader_id ||
    preferences?.downloader_id ||
    preferences?.usenet_downloader_id ||
    protocolPreference(preferences, "torrent", downloaders) ||
    protocolPreference(preferences, "nzb", downloaders) ||
    chooseRoute(downloaders.filter((item) => item.protocol === "soulseek"))?.id
  );
}

export function downloaderLabel(item: { name: string; protocol?: string }) {
  if (item.protocol === "nzb") return `${item.name} · Usenet`;
  if (item.protocol === "torrent") return `${item.name} · Torrents`;
  return item.name;
}

export function chooseRoute<T extends { id: string }>(
  items: T[],
  selected?: string | null,
  preferred?: string | null,
) {
  const id = selected || preferred;
  return id
    ? items.find((item) => item.id === id)
    : items.length === 1
      ? items[0]
      : undefined;
}

export function destinationPreference(
  preferences: Partial<Preferences> | undefined,
  medium: string,
  destinations: DestinationChoice[] = [],
) {
  const saved =
    medium === "audio"
      ? preferences?.audio_destination_id
      : preferences?.ebook_destination_id;
  const choices = libraryDestinations(preferences, medium, destinations);
  // Preserve unavailable saved choices, but a selected library takes precedence
  // over a valid default belonging to another library.
  const savedChoice = destinations.find((item) => item.id === saved);
  if (
    saved &&
    (!savedChoice ||
      savedChoice.medium !== medium ||
      choices.some((item) => item.id === saved))
  )
    return saved;
  return choices.length === 1 ? choices[0].id : undefined;
}

function libraryDestinations<T extends DestinationChoice>(
  preferences: Partial<Preferences> | undefined,
  medium: string,
  destinations: T[],
) {
  const library =
    medium === "audio"
      ? preferences?.audio_library_id
      : preferences?.ebook_library_id;
  return destinations.filter(
    (item) =>
      item.medium === medium && (!library || item.library_id === library),
  );
}

function useOptions(enabled: boolean) {
  return useQuery({
    queryKey: ["selection-options"],
    enabled,
    queryFn: async () =>
      result(await api.GET("/api/acquisition/selections/options")),
  });
}

export default function RouteFields({
  overrides,
  inherited,
  origins,
  onChange,
}: {
  overrides: Overrides;
  inherited: Preferences;
  origins: Record<string, string>;
  onChange: (value: Overrides) => void;
}) {
  const [open, setOpen] = useState(false);
  const options = useOptions(open);
  const values = { ...inherited, ...overrides };
  return (
    <details onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        <span className="setting-subheading">
          Downloader defaults
          <SettingHelp label="download preferences">
            The only enabled client of each type is used automatically. When
            several clients share a type, choose a default here. Soulseek uses
            its own connection for searches and downloads. Destinations use the
            folders configured in Libraries, separately for ebooks and
            audiobooks. Download folders remain configured on each client.
            Automatic imports still require a verified connection between those
            folders. Saved requests keep their accepted choices.
          </SettingHelp>
        </span>
      </summary>

      <p className="muted">
        Finished books use the folders configured in Libraries. Each download
        client keeps its own download folder; Dewarr imports from there into
        your ebook or audiobook library.
      </p>
      <Notice error={options.error} />
      {downloaderFields.map(({ field, protocol }) => {
        const downloaders = options.data?.downloaders || [];
        const choices = downloaders.filter(
          (item) => item.protocol === protocol,
        );
        const selected =
          protocolPreference(values, protocol, downloaders) || "";
        const automatic = choices.length === 1 && selected === choices[0].id;
        return (
          <div key={field}>
            <label>
              {routeLabels[field]}
              <select
                aria-label={routeLabels[field]}
                value={selected}
                disabled={options.isPending || automatic}
                onChange={(event) => {
                  const raw = event.target.value;
                  const next = {
                    ...overrides,
                    [field]: raw || null,
                  };
                  if (
                    !raw &&
                    downloaders.find((item) => item.id === values.downloader_id)
                      ?.protocol === protocol
                  )
                    next.downloader_id = null;
                  onChange(next);
                }}
              >
                <option value="">
                  {choices.length > 1
                    ? "Choose a default"
                    : "No client configured"}
                </option>
                {selected && !choices.some((item) => item.id === selected) && (
                  <option value={selected}>Unavailable saved choice</option>
                )}
                {choices.map((item) => (
                  <option key={item.id} value={item.id}>
                    {downloaderLabel(item)}
                    {item.ready ? "" : " · needs verification"}
                  </option>
                ))}
              </select>
            </label>
            {automatic && (
              <p className="muted">
                Used automatically · your only{" "}
                {protocol === "torrent" ? "torrent" : "Usenet"} client.
              </p>
            )}
            {Object.hasOwn(overrides, field) && (
              <div className="preference-origin">
                <SettingHelp label="inherited value">
                  {Object.hasOwn(overrides, field)
                    ? "Custom value"
                    : `Inherited · ${origins[field] || "no default"}`}
                </SettingHelp>
                {Object.hasOwn(overrides, field) && (
                  <button
                    type="button"
                    aria-label={`Use inherited ${routeLabels[field]}`}
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
            )}
          </div>
        );
      })}
    </details>
  );
}

export function EffectiveRoutes({
  preferences,
  origins,
}: {
  preferences: Preferences;
  origins: Record<string, string>;
}) {
  const options = useOptions(true);
  const downloaders = options.data?.downloaders || [];
  const fields = (Object.keys(routeLabels) as Field[]).filter((field) => {
    const protocol = protocolFor(field);
    return (
      preferences[field] ||
      origins[field] ||
      (protocol
        ? protocolPreference(preferences, protocol, downloaders)
        : destinationPreference(
            preferences,
            field === "audio_destination_id" ? "audio" : "ebook",
            options.data?.destinations,
          ))
    );
  });
  if (!fields.length) return null;
  return (
    <dl>
      {fields.map((field) => {
        const protocol = protocolFor(field);
        const selected = protocol
          ? protocolPreference(preferences, protocol, downloaders)
          : destinationPreference(
              preferences,
              field === "audio_destination_id" ? "audio" : "ebook",
              options.data?.destinations,
            );
        const choices = protocol ? downloaders : options.data?.destinations;
        const name = selected
          ? protocol
            ? downloaderLabel(
                choices?.find((item) => item.id === selected) || {
                  name: "Unavailable saved choice",
                },
              )
            : choices?.find((item) => item.id === selected)?.name ||
              "Unavailable saved choice"
          : "No saved default";
        return (
          <div key={field}>
            <dt>{routeLabels[field]}</dt>
            <dd>
              {name}
              <small>
                {" "}
                ·{" "}
                {origins[field] ||
                  (protocol
                    ? origins.downloader_id || "Automatic client default"
                    : preferences[field]
                      ? "Saved profile"
                      : "Configured library folder")}
              </small>
            </dd>
          </div>
        );
      })}
    </dl>
  );
}
