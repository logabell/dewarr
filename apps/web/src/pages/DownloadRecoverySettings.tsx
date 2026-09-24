import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";

type Configuration = components["schemas"]["RecoveryConfiguration"];
type Policy = components["schemas"]["RecoveryPolicy"];
const defaultPolicy: Policy = {
  enabled: true,
  stall_hours: 24,
  error_minutes: 5,
  cleanup: "leave",
};

export default function DownloadRecoverySettings() {
  const cache = useQueryClient();
  const [offset, setOffset] = useState(0);
  const [saved, setSaved] = useState(false);
  const approvals = useQuery({
    queryKey: ["recovery-approvals"],
    queryFn: async () =>
      result(await api.GET("/api/acquisition/recovery/approvals")),
    refetchInterval: 15000,
  });
  const approve = useMutation({
    mutationFn: async (id: string) =>
      result(
        await api.POST("/api/acquisition/recovery/{recovery_id}/approve", {
          params: { path: { recovery_id: id } },
        }),
      ),
    onSuccess: async () => {
      await Promise.all(
        ["recovery-approvals", "downloads", "requests"].map((key) =>
          cache.invalidateQueries({ queryKey: [key] }),
        ),
      );
    },
  });
  const settings = useQuery({
    queryKey: ["download-recovery-settings"],
    queryFn: async () =>
      result(await api.GET("/api/acquisition/recovery/settings")),
  });
  const blocks = useQuery({
    queryKey: ["release-blocklist", offset],
    queryFn: async () =>
      result(
        await api.GET("/api/acquisition/recovery/blocklist", {
          params: { query: { offset, limit: 50 } },
        }),
      ),
  });
  const remove = useMutation({
    mutationFn: async (block_id: string) =>
      result(
        await api.DELETE("/api/acquisition/recovery/blocklist/{block_id}", {
          params: { path: { block_id } },
        }),
      ),
    onSuccess: () =>
      cache.invalidateQueries({ queryKey: ["release-blocklist"] }),
  });
  return (
    <div className="policy-settings">
      <p>
        Recover stalled or failed downloads using the next eligible release.
        Original request requirements and transfer limits still apply.
      </p>
      <Notice
        error={
          settings.error ||
          blocks.error ||
          remove.error ||
          approvals.error ||
          approve.error
        }
      />
      {settings.isPending && <Loading />}
      {settings.data && (
        <Editor
          key={settings.dataUpdatedAt}
          current={settings.data}
          onSaved={(value) => {
            cache.setQueryData(["download-recovery-settings"], value);
            setSaved(true);
          }}
        />
      )}
      {saved && <p role="status">Recovery settings saved.</p>}
      <details className="policy-disclosure" open={!!approvals.data?.length}>
        <summary>
          Replacement approvals
          {approvals.data?.length ? ` (${approvals.data.length})` : ""}
        </summary>
        <div className="policy-stack">
          {approvals.isPending && <Loading />}
          {approvals.data?.length === 0 && (
            <p>No replacements waiting for approval.</p>
          )}
          <ul className="policy-records" aria-label="Replacement approvals">
            {approvals.data?.map((item) => (
              <li key={item.id}>
                <strong>{item.work_title}</strong>
                <p>{item.reason}</p>
                <button
                  disabled={approve.isPending}
                  onClick={() => approve.mutate(item.id)}
                >
                  Approve replacement
                </button>
              </li>
            ))}
          </ul>
        </div>
      </details>
      <details className="policy-disclosure">
        <summary>Release blocklist</summary>
        <div className="policy-stack">
          {blocks.isPending && <Loading />}
          <p>
            These releases are excluded for the listed book and format. Removing
            an entry allows future selection; recorded transfers are never added
            again.
          </p>
          {blocks.data?.length === 0 && (
            <p>No blocked releases on this page.</p>
          )}
          <ul className="policy-records" aria-label="Blocked releases">
            {blocks.data?.map((block) => (
              <li key={block.id}>
                <strong>{block.title}</strong> · {block.source} · {block.medium}
                <p>Book: {block.work_title}</p>
                <p>
                  {block.reason} ·{" "}
                  {block.automatic ? "Automatic detection for" : "Reported by"}{" "}
                  {block.actor_name} ·{" "}
                  {new Date(block.created_at).toLocaleString()}
                </p>
                <button
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(block.id)}
                >
                  Remove from blocklist
                </button>
              </li>
            ))}
          </ul>
          <div className="button-row">
            <button
              disabled={blocks.isFetching || offset === 0}
              onClick={() => setOffset(Math.max(0, offset - 50))}
            >
              Previous
            </button>
            <button
              disabled={blocks.isFetching || (blocks.data?.length ?? 0) < 50}
              onClick={() => setOffset(offset + 50)}
            >
              Next
            </button>
          </div>
        </div>
      </details>
    </div>
  );
}

function Editor({
  current,
  onSaved,
}: {
  current: Configuration;
  onSaved: (value: Configuration) => void;
}) {
  const [value, setValue] = useState(current);
  const [source, setSource] = useState("prowlarr");
  const [indexer, setIndexer] = useState("");
  const sourceKey = source === "indexer" ? `prowlarr:${indexer}` : source;
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/acquisition/recovery/settings", { body: value }),
      ),
    onSuccess: onSaved,
  });
  return (
    <form
      aria-label="Download recovery settings"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <Notice error={save.error} />
      <fieldset className="policy-section" disabled={save.isPending}>
        <legend>Replacement rules</legend>
        <div className="policy-fields">
          <label>
            Maximum attempts per requested format
            <input
              type="number"
              min={1}
              max={20}
              required
              value={value.attempt_cap}
              onChange={(event) =>
                setValue({ ...value, attempt_cap: Number(event.target.value) })
              }
            />
            <span className="field-hint">
              Includes the first download. Between 1 and 20 attempts.
            </span>
          </label>
          <label className="check-label">
            <input
              type="checkbox"
              checked={value.approve_reports}
              onChange={(event) =>
                setValue({ ...value, approve_reports: event.target.checked })
              }
            />
            Require administrator approval for reported problems
          </label>
        </div>
      </fieldset>
      <PolicyFields
        name="Automatic recovery defaults"
        value={value.defaults ?? defaultPolicy}
        change={(defaults) => setValue({ ...value, defaults })}
      />
      <details
        className="policy-disclosure"
        open={Object.keys(current.sources ?? {}).length > 0}
      >
        <summary>Source overrides</summary>
        <div className="policy-stack">
          <p>
            MyAnonamouse stall detection is off by default. An override replaces
            the installation defaults for that source.
          </p>
          {Object.entries(value.sources ?? {}).map(([key, policy]) => (
            <div className="policy-override" key={key}>
              <PolicyFields
                name={sourceName(key)}
                value={policy}
                change={(next) =>
                  setValue({
                    ...value,
                    sources: { ...value.sources, [key]: next },
                  })
                }
              />
              <button
                type="button"
                onClick={() => {
                  const sources = { ...value.sources };
                  delete sources[key];
                  setValue({ ...value, sources });
                }}
              >
                Use defaults for {sourceName(key)}
              </button>
            </div>
          ))}
          <div className="policy-add-row">
            <label>
              Source
              <select
                value={source}
                onChange={(event) => setSource(event.target.value)}
              >
                <option value="prowlarr">Prowlarr — all indexers</option>
                <option value="indexer">Prowlarr — specific indexer</option>
                <option value="mam">MyAnonamouse</option>
                <option value="audiobookbay">AudiobookBay</option>
                <option value="slskd">Soulseek</option>
              </select>
            </label>
            {source === "indexer" && (
              <label>
                Prowlarr indexer ID
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={indexer}
                  onChange={(event) => setIndexer(event.target.value)}
                />
              </label>
            )}
            <button
              type="button"
              disabled={
                !/^(mam|prowlarr(:[1-9][0-9]*)?|audiobookbay|slskd)$/.test(
                  sourceKey,
                ) || sourceKey in (value.sources ?? {})
              }
              onClick={() =>
                setValue({
                  ...value,
                  sources: {
                    ...value.sources,
                    [sourceKey]: { ...(value.defaults ?? defaultPolicy) },
                  },
                })
              }
            >
              Add source override
            </button>
          </div>
        </div>
      </details>
      <p>
        Cleanup leaves transfers alone by default. Pause and remove are
        supported for qBittorrent only; remove preserves all downloaded and
        library files. A transfer left running continues to occupy capacity.
      </p>
      <div className="button-row">
        <button disabled={save.isPending}>
          {save.isPending ? "Saving…" : "Save recovery settings"}
        </button>
      </div>
    </form>
  );
}

function PolicyFields({
  name,
  value,
  change,
}: {
  name: string;
  value: Policy;
  change: (next: Policy) => void;
}) {
  return (
    <fieldset className="policy-section">
      <legend>{name}</legend>
      <label>
        <input
          type="checkbox"
          checked={value.enabled ?? true}
          onChange={(event) =>
            change({ ...value, enabled: event.target.checked })
          }
        />
        Enable automatic recovery
      </label>
      <div className="policy-fields policy-fields-three">
        <label>
          No progress or zero seeders (hours)
          <input
            type="number"
            min={0.01}
            max={8760}
            step="any"
            value={value.stall_hours ?? ""}
            onChange={(event) =>
              change({
                ...value,
                stall_hours: event.target.value
                  ? Number(event.target.value)
                  : null,
              })
            }
          />
        </label>
        <label>
          Downloader error grace period (minutes)
          <input
            type="number"
            min={0}
            max={10080}
            step="any"
            required
            value={value.error_minutes ?? 5}
            onChange={(event) =>
              change({ ...value, error_minutes: Number(event.target.value) })
            }
          />
        </label>
        <label>
          Failed transfer cleanup
          <select
            value={value.cleanup ?? "leave"}
            onChange={(event) =>
              change({
                ...value,
                cleanup: event.target.value as Policy["cleanup"],
              })
            }
          >
            <option value="leave">Leave in client</option>
            <option value="pause">Pause in client</option>
            <option value="remove">Remove from client, preserve files</option>
          </select>
        </label>
      </div>
      <p className="muted">
        Leave the stall threshold blank to disable stall detection. Error
        recovery still uses the grace period.
      </p>
    </fieldset>
  );
}

function sourceName(source: string) {
  const names: Record<string, string> = {
    mam: "MyAnonamouse",
    prowlarr: "Prowlarr — all indexers",
    audiobookbay: "AudiobookBay",
    slskd: "Soulseek",
  };
  return (
    names[source] ??
    (source.startsWith("prowlarr:")
      ? `Prowlarr — indexer ${source.split(":")[1]}`
      : source)
  );
}
