import { DeleteSourceConnection } from "../components/DeleteConfiguration";
import SettingHelp from "../components/SettingHelp";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Link } from "react-router-dom";
import { Loading, Notice } from "../components";
import ConnectionTestStatus, {
  SavedSecretIndicator,
} from "../components/ConnectionTestStatus";

type Connection = components["schemas"]["SlskdConnectionView"];

export function SlskdConnectionForm({
  value,
  onConfigureFolder,
}: {
  value: Connection;
  onConfigureFolder?: () => void;
}) {
  const cache = useQueryClient();
  const [url, setUrl] = useState(value.base_url);
  const [apiKey, setApiKey] = useState("");
  const [enabled, setEnabled] = useState(value.enabled || !value.configured);
  const refresh = () =>
    Promise.all([
      cache.invalidateQueries({ queryKey: ["slskd-connection"] }),
      cache.invalidateQueries({ queryKey: ["downloaders"] }),
      cache.invalidateQueries({ queryKey: ["setup-readiness"] }),
      cache.invalidateQueries({ queryKey: ["library-folder-options"] }),
    ]);
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/sources/slskd/connection", {
          body: {
            base_url: url,
            api_key: apiKey || null,
            enabled,
            expected_generation: value.generation,
          },
        }),
      ),
    onSuccess: (connection) => {
      setApiKey("");
      cache.setQueryData(["slskd-connection"], connection);
      if (connection.enabled) test.mutate();
      else refresh();
    },
  });
  const test = useMutation({
    mutationFn: async () =>
      result(await api.POST("/api/sources/slskd/connection/test")),
    onSuccess: (connection) =>
      cache.setQueryData(["slskd-connection"], connection),
    onSettled: refresh,
  });
  return (
    <form
      className="panel editor"
      aria-label="Soulseek connection settings"
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <p>
        <SettingHelp label="Soulseek connection">
          slskd is both the search source and the downloader. Soulseek login
          stays in slskd. Dewarr uses a read-write API key, reads the download
          folder, and checks whether Dewarr can access the same path. If the
          paths differ, configure the folder mapping under Download clients.
        </SettingHelp>
      </p>
      <label>
        slskd URL
        <input
          type="url"
          required
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="http://127.0.0.1:5030"
          maxLength={2000}
        />
      </label>
      <label>
        <span className="credential-label">
          <span>API key</span>
          <SavedSecretIndicator saved={value.has_api_key} />
        </span>
        <input
          aria-label="API key"
          type="password"
          value={apiKey}
          onChange={(event) => setApiKey(event.target.value)}
          autoComplete="off"
          placeholder={value.has_api_key ? "••••••••" : "Read-write API key"}
          minLength={apiKey ? 16 : undefined}
          maxLength={255}
        />
      </label>
      <label className="check-label">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(event) => setEnabled(event.target.checked)}
        />
        Enabled
      </label>
      <div className="connection-action-bar">
        <div className="button-row">
          {value.configured && (
            <DeleteSourceConnection
              source="slskd"
              name="Soulseek"
              generation={value.generation}
              disabled={save.isPending || test.isPending}
            />
          )}

          <button type="submit" disabled={save.isPending || test.isPending}>
            {save.isPending ? "Saving…" : "Save & test connection"}
          </button>
          <button
            type="button"
            disabled={test.isPending || save.isPending || !value.configured}
            onClick={() => test.mutate()}
          >
            {test.isPending ? "Testing…" : "Test connection"}
          </button>
        </div>
        <ConnectionTestStatus
          configured={value.configured}
          status={value.status}
          lastSuccessAt={value.last_success_at}
          isPending={test.isPending}
          error={test.error}
        />
      </div>
      <Notice error={save.error || test.error} />
      {value.download_root && (
        <div className="slskd-folder-status">
          <p>
            Download folder: <code>{value.download_root}</code>
          </p>
          <p>
            {value.mapped
              ? "Folder mapping configured. Library setup verifies file access before importing."
              : "Folder setup is still needed. Mount this folder at the same path in Dewarr, or choose its corresponding local folder in Download clients."}
          </p>
          <Link to="/settings#downloaders" onClick={onConfigureFolder}>
            Configure download folder
          </Link>
        </div>
      )}
    </form>
  );
}

export default function SlskdSettings({
  onConfigureFolder,
}: {
  onConfigureFolder?: () => void;
}) {
  const query = useQuery({
    queryKey: ["slskd-connection"],
    queryFn: async () => result(await api.GET("/api/sources/slskd/connection")),
  });
  return (
    <>
      <Notice error={query.error} />
      {query.isPending ? (
        <Loading />
      ) : (
        query.data && (
          <SlskdConnectionForm
            key={query.data.generation}
            value={query.data}
            onConfigureFolder={onConfigureFolder}
          />
        )
      )}
    </>
  );
}
