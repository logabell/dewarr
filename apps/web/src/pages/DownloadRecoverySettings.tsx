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
    <div className="panel editor">
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
      <h3>Replacement approvals</h3>
      {approvals.data?.length === 0 && (
        <p>No replacements waiting for approval.</p>
      )}
      <ul aria-label="Replacement approvals">
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
      <h3>Release blocklist</h3>
      <p>
        These releases are excluded for the listed book and format. Removing an
        entry allows future selection; recorded transfers are never added again.
      </p>
      {blocks.data?.length === 0 && <p>No blocked releases on this page.</p>}
      <ul aria-label="Blocked releases">
        {blocks.data?.map((block) => (
          <li key={block.id}>
            <strong>{block.title}</strong> · {block.source} · {block.medium}
            <p>Book: {block.work_title}</p>
            <p>
              {block.reason} ·{" "}
              {block.automatic ? "Automatic detection for" : "Reported by"}{" "}
              {block.actor_name} · {new Date(block.created_at).toLocaleString()}
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
          disabled={offset === 0}
          onClick={() => setOffset(Math.max(0, offset - 50))}
        >
          Previous
        </button>
        <button
          disabled={(blocks.data?.length ?? 0) < 50}
          onClick={() => setOffset(offset + 50)}
        >
          Next
        </button>
      </div>
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
      <label>
        Maximum attempts per requested format (including the first download)
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
      </label>
      <label>
        <input
          type="checkbox"
          checked={value.approve_reports}
          onChange={(event) =>
            setValue({ ...value, approve_reports: event.target.checked })
          }
        />
        Require administrator approval for reported problems
      </label>
      <PolicyFields
        name="Installation defaults"
        value={value.defaults ?? defaultPolicy}
        change={(defaults) => setValue({ ...value, defaults })}
      />
      <h3>Source overrides</h3>
      <p>
        MyAnonamouse stall detection is off by default. An override replaces the
        installation defaults for that source.
      </p>
      {Object.entries(value.sources ?? {}).map(([key, policy]) => (
        <fieldset key={key}>
          <PolicyFields
            name={key}
            value={policy}
            change={(next) =>
              setValue({ ...value, sources: { ...value.sources, [key]: next } })
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
            Use installation defaults for {key}
          </button>
        </fieldset>
      ))}
      <label>
        Source or Prowlarr indexer (for example prowlarr:12)
        <input
          value={source}
          pattern="mam|prowlarr(:[1-9][0-9]*)?|audiobookbay|slskd"
          onChange={(event) => setSource(event.target.value)}
        />
      </label>
      <button
        type="button"
        disabled={
          !/^(mam|prowlarr(:[1-9][0-9]*)?|audiobookbay|slskd)$/.test(source) ||
          source in (value.sources ?? {})
        }
        onClick={() =>
          setValue({
            ...value,
            sources: {
              ...value.sources,
              [source]: value.defaults ?? defaultPolicy,
            },
          })
        }
      >
        Add source override
      </button>
      <p>
        Cleanup leaves transfers alone by default. Pause and remove are
        supported for qBittorrent only; remove preserves all downloaded and
        library files. A transfer left running continues to occupy capacity.
      </p>
      <button disabled={save.isPending}>Save recovery settings</button>
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
    <fieldset>
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
      <label>
        No progress or zero seeders (hours; leave blank to disable)
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
    </fieldset>
  );
}
