import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Notice } from "../components";

type View = components["schemas"]["CapacityView"];

export default function CapacitySettings() {
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState(false);
  const cache = useQueryClient();
  const settings = useQuery({
    queryKey: ["capacity-settings"],
    queryFn: async () => result(await api.GET("/api/acquisition/capacity")),
    enabled: open,
  });
  return (
    <details
      className="panel editor"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>Storage reserve</summary>
      <p>
        Downloads have no daily or concurrent transfer cap. Storage is checked
        before downloading and importing.
      </p>
      <Notice error={settings.error} />
      {saved && <p role="status">Storage reserve saved.</p>}
      {settings.data && (
        <>
          <p>
            {settings.data.occupied_slots} active transfers ·{" "}
            {(settings.data.reserved_bytes / 1024 ** 3).toFixed(2)} GiB reserved
            across filesystems
          </p>
          <Editor
            key={settings.data.revision}
            current={settings.data}
            onSaved={(value) => {
              cache.setQueryData(["capacity-settings"], value);
              setSaved(true);
            }}
          />
        </>
      )}
    </details>
  );
}

function Editor({
  current,
  onSaved,
}: {
  current: View;
  onSaved: (value: View) => void;
}) {
  const [limits, setLimits] = useState(current.limits);
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/acquisition/capacity", {
          body: { limits, expected_revision: current.revision },
        }),
      ),
    onSuccess: onSaved,
  });
  return (
    <form
      aria-label="Storage reserve"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <Notice error={save.error} />
      <label>
        Minimum free storage (GiB)
        <input
          type="number"
          required
          min={0}
          max={1000000}
          step={0.25}
          value={limits.minimum_free_bytes / 1024 ** 3}
          onChange={(e) =>
            setLimits({
              ...limits,
              minimum_free_bytes: Math.round(
                Number(e.target.value) * 1024 ** 3,
              ),
            })
          }
        />
      </label>
      <label>
        Minimum free storage (%)
        <input
          type="number"
          required
          min={0}
          max={50}
          value={limits.minimum_free_percent}
          onChange={(e) =>
            setLimits({
              ...limits,
              minimum_free_percent: Number(e.target.value),
            })
          }
        />
      </label>
      <p className="muted">
        Keep the larger free-space reserve on each filesystem. Hardlinks share
        media bytes; copies reserve extra space. Storage that cannot be measured
        pauses new downloads.
      </p>
      <button className="primary" disabled={save.isPending}>
        {save.isPending ? "Saving storage reserve…" : "Save storage reserve"}
      </button>
    </form>
  );
}
