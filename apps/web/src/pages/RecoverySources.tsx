import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
import type { components } from "../api/schema";
import { randomUUID } from "../randomUUID";

export type SourceReview = components["schemas"]["SourceReconciliationView"];
type Choice = components["schemas"]["SourceChoice"];
type Settings = components["schemas"]["RecoverySourceSettings"];
type Evidence = {
  source_schema: number;
  before: Settings;
  requires_current_session: boolean;
  downloaders: { id: string; name: string; enabled: boolean }[];
  blocked_until: string | null;
};
const names = {
  mam: "MyAnonamouse",
  prowlarr: "Prowlarr",
  audiobookbay: "AudiobookBay",
};

export function PrepareSourceReview({
  scanId,
  findingId,
  title,
  disabled,
}: {
  scanId: string;
  findingId: string;
  title: string;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const observation = useQuery({
    queryKey: ["recovery-source", scanId, findingId],
    enabled: open,
    queryFn: async () =>
      result(
        await api.GET("/api/recovery/scans/{scan_id}/findings/{finding_id}", {
          params: { path: { scan_id: scanId, finding_id: findingId } },
        }),
      ),
  });
  const evidence = observation.data?.evidence as Evidence | undefined;
  return (
    <>
      <button
        disabled={disabled}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        {open ? `Close settings for ${title}` : `Review settings for ${title}`}
      </button>
      {open && (
        <>
          {observation.isPending && <Loading />}
          <Notice error={observation.error} />
          {evidence?.source_schema === 1 && !observation.isError && (
            <SourceEditor
              key={findingId}
              evidence={evidence}
              scanId={scanId}
              findingId={findingId}
              disabled={disabled}
              onPrepared={() => setOpen(false)}
            />
          )}
        </>
      )}
    </>
  );
}

function SourceEditor({
  evidence,
  scanId,
  findingId,
  disabled,
  onPrepared,
}: {
  evidence: Evidence;
  scanId: string;
  findingId: string;
  disabled: boolean;
  onPrepared: () => void;
}) {
  const client = useQueryClient();
  const [choice, setChoice] = useState<Choice>(() => ({
    finding_id: findingId,
    source_key: evidence.before.source_key,
    base_url: evidence.before.base_url,
    proxy_url: evidence.before.proxy_url,
    proxy_fallback_direct: evidence.before.proxy_fallback_direct,
    enabled: evidence.before.enabled,
    clear_proxy_credentials: false,
    excluded_indexers: evidence.before.excluded_indexers,
    metadata_downloader_id: evidence.before.metadata_downloader_id,
  }));
  const [key, setKey] = useState(() => randomUUID());
  const [excluded, setExcluded] = useState(
    evidence.before.excluded_indexers.join(", "),
  );
  function edit(patch: Partial<Choice>) {
    setChoice((previous) => ({ ...previous, ...patch }));
    setKey(randomUUID());
  }
  const prepare = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/recovery/source-reconciliations", {
          params: { header: { "idempotency-key": key } },
          body: { scan_id: scanId, change: choice },
        }),
      ),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["recovery"] });
      onPrepared();
    },
  });
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        prepare.mutate();
      }}
    >
      <fieldset disabled={disabled || prepare.isPending}>
        <legend>Settings for {names[choice.source_key]}</legend>
        <p>
          Confirmation saves the reviewed settings, then tests an enabled
          source. A failed test leaves the settings saved so a rotated MAM
          session is preserved.
        </p>
        <label>
          Source URL
          <input
            required
            type="url"
            maxLength={2000}
            value={choice.base_url}
            onChange={(e) => edit({ base_url: e.target.value })}
          />
        </label>
        <label className="difference-choice">
          <input
            type="checkbox"
            checked={choice.enabled}
            onChange={(e) => edit({ enabled: e.target.checked })}
          />
          Enable source
        </label>
        {choice.source_key === "mam" && (
          <>
            <p>
              {evidence.requires_current_session
                ? "Enter a current mam_id before enabling or testing this restored session."
                : "Leave mam_id blank to keep the session supplied during this recovery. A changed route or interrupted request needs a current mam_id."}
            </p>
            <label>
              Current mam_id
              <input
                type="password"
                autoComplete="new-password"
                maxLength={8192}
                required={choice.enabled && evidence.requires_current_session}
                value={choice.mam_id ?? ""}
                onChange={(e) => edit({ mam_id: e.target.value || null })}
              />
            </label>
          </>
        )}
        {choice.source_key === "prowlarr" ? (
          <>
            <p>
              Leave the API key blank to keep it. A changed endpoint needs a
              fresh key. Indexer proxy routing is configured in Prowlarr.
            </p>
            <label>
              New API key
              <input
                type="password"
                autoComplete="new-password"
                maxLength={1000}
                value={choice.api_key ?? ""}
                onChange={(e) => edit({ api_key: e.target.value || null })}
              />
            </label>
            <label>
              Excluded indexer IDs
              <input
                value={excluded}
                placeholder="7, 12"
                pattern="\s*([1-9][0-9]*\s*(,\s*[1-9][0-9]*\s*)*)?"
                onChange={(e) => {
                  setExcluded(e.target.value);
                  edit({
                    excluded_indexers: e.target.value.trim()
                      ? e.target.value.split(",").map((v) => Number(v.trim()))
                      : [],
                  });
                }}
              />
            </label>
          </>
        ) : (
          <>
            <label>
              Proxy URL
              <input
                type="url"
                maxLength={2000}
                value={choice.proxy_url ?? ""}
                onChange={(e) => edit({ proxy_url: e.target.value || null })}
              />
            </label>
            <p>
              {choice.source_key === "mam" && choice.proxy_fallback_direct
                ? "MAM prefers the configured proxy and falls back direct when it is unavailable."
                : "A configured proxy is required for every source request."}{" "}
              Changing its URL clears saved proxy credentials unless you replace
              them below.
            </p>
            {choice.source_key === "mam" && (
              <label className="difference-choice">
                <input
                  type="checkbox"
                  checked={choice.proxy_fallback_direct}
                  onChange={(e) =>
                    edit({ proxy_fallback_direct: e.target.checked })
                  }
                />
                Allow direct fallback when the proxy is unavailable
              </label>
            )}
            <label>
              New proxy username
              <input
                autoComplete="off"
                maxLength={300}
                value={choice.proxy_username ?? ""}
                onChange={(e) =>
                  edit({ proxy_username: e.target.value || null })
                }
              />
            </label>
            <label>
              New proxy password
              <input
                type="password"
                autoComplete="new-password"
                maxLength={1000}
                value={choice.proxy_password ?? ""}
                onChange={(e) =>
                  edit({ proxy_password: e.target.value || null })
                }
              />
            </label>
            <label className="difference-choice">
              <input
                type="checkbox"
                checked={choice.clear_proxy_credentials ?? false}
                onChange={(e) =>
                  edit({ clear_proxy_credentials: e.target.checked })
                }
              />
              Clear saved proxy credentials
            </label>
          </>
        )}
        {choice.source_key === "audiobookbay" && (
          <>
            <label>
              Metadata downloader
              <select
                value={choice.metadata_downloader_id ?? ""}
                onChange={(e) =>
                  edit({ metadata_downloader_id: e.target.value || null })
                }
              >
                <option value="">Not configured</option>
                {choice.metadata_downloader_id &&
                  !evidence.downloaders.some(
                    (d) => d.id === choice.metadata_downloader_id,
                  ) && (
                    <option value={choice.metadata_downloader_id}>
                      Saved downloader unavailable
                    </option>
                  )}
                {evidence.downloaders.map((d) => (
                  <option key={d.id} value={d.id} disabled={!d.enabled}>
                    {d.name}
                    {d.enabled ? "" : " (disabled)"}
                  </option>
                ))}
              </select>
            </label>
            <p>
              The source test checks the site only. Torrent metadata resolution
              and downloader compatibility need separate qualification.
            </p>
          </>
        )}
        {evidence.blocked_until &&
          new Date(evidence.blocked_until).getTime() > Date.now() && (
            <p>
              Source cooldown until{" "}
              {new Date(evidence.blocked_until).toLocaleString()}. Saving
              settings does not bypass it.
            </p>
          )}
        <button type="submit">
          {prepare.isPending
            ? "Preparing source preview…"
            : "Preview source settings"}
        </button>
      </fieldset>
      <Notice error={prepare.error} />
    </form>
  );
}

function SettingsSummary({ value }: { value: Settings }) {
  return (
    <>
      <p>
        {names[value.source_key]} · {value.enabled ? "Enabled" : "Disabled"}
      </p>
      <p>Source: {value.base_url}</p>
      <p>
        Route:{" "}
        {value.proxy_url ? `Required proxy · ${value.proxy_url}` : "Direct"}
      </p>
      {value.source_key === "prowlarr" && (
        <p>Excluded indexers: {value.excluded_indexers.join(", ") || "None"}</p>
      )}
      {value.source_key === "audiobookbay" && (
        <p>
          Metadata downloader:{" "}
          {value.metadata_downloader_id || "Not configured"}
        </p>
      )}
    </>
  );
}

export function SourceRecoveryReview({
  review,
  currentScan,
  otherBusy,
}: {
  review: SourceReview;
  currentScan?: string;
  otherBusy: boolean;
}) {
  const client = useQueryClient();
  const [key] = useState(() => randomUUID());
  const heading = useRef<HTMLHeadingElement>(null);
  const prepared = review.status === "prepared";
  useEffect(() => {
    if (prepared) heading.current?.focus();
  }, [review.id, prepared]);
  const accept = useMutation({
    mutationFn: async () =>
      result(
        await api.POST(
          "/api/recovery/source-reconciliations/{identifier}/accept",
          {
            params: {
              path: { identifier: review.id },
              header: { "idempotency-key": key },
            },
            body: { revision: review.revision },
          },
        ),
      ),
    onSuccess: () => client.invalidateQueries({ queryKey: ["recovery"] }),
  });
  return (
    <section
      className="recovery-decision"
      aria-labelledby="source-review-title"
    >
      <h3 id="source-review-title" ref={heading} tabIndex={-1}>
        Review source settings
      </h3>
      <p>
        Confirmation saves these settings, then verifies an enabled source.
        Verification can rotate MAM credentials; a failed test keeps the saved
        settings and any returned session. Disabled sources are saved without
        contacting them.
      </p>
      <ul>
        {review.items.map((item) => (
          <li key={item.source_key}>
            <h4>Saved settings</h4>
            <SettingsSummary value={item.before} />
            <h4>Reviewed settings</h4>
            <SettingsSummary value={item.after} />
            {item.source_key !== "audiobookbay" && (
              <p>
                Source credentials:{" "}
                {item.replace_credentials
                  ? "replace with newly entered credentials"
                  : "keep saved credentials"}
                .
              </p>
            )}
            <p>
              Proxy credentials:{" "}
              {item.proxy_credentials === "replace"
                ? "replace with newly entered credentials"
                : item.proxy_credentials === "clear"
                  ? "clear saved credentials"
                  : "keep saved credentials"}
              .
            </p>
          </li>
        ))}
      </ul>
      <p role="status">{review.message}</p>
      {review.verification && (
        <div role="status">
          <h4>Connection verification · {review.verification.status}</h4>
          <p>{review.verification.message}</p>
          {review.verification.status === "held" && (
            <p>
              Start a fresh observation, review the source settings and confirm
              to test again. Source cooldowns still apply.
            </p>
          )}
        </div>
      )}
      <p>Downloads, imports and automation remain paused.</p>
      {prepared && (
        <>
          <p className="muted">
            Review expires {new Date(review.expires_at).toLocaleString()}.
          </p>
          {review.scan_id !== currentScan && (
            <p>A newer observation exists. Prepare a new review from it.</p>
          )}
          <button
            disabled={
              accept.isPending || otherBusy || review.scan_id !== currentScan
            }
            onClick={() => accept.mutate()}
          >
            {accept.isPending
              ? "Saving reviewed settings…"
              : "Confirm reviewed source settings"}
          </button>
        </>
      )}
      <Notice error={accept.error} />
    </section>
  );
}
