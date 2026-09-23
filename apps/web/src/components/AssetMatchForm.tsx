import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Notice } from "../components";
import IdentityHistory from "../pages/IdentityHistory";

type Asset = components["schemas"]["AssetView"];

export function useAssetMatch(
  asset: Asset,
  done: (workId: string | null) => void,
) {
  const cache = useQueryClient();
  return useMutation({
    mutationFn: async (id: string | null) =>
      result(
        await api.POST("/api/library/assets/{asset_id}/match", {
          params: { path: { asset_id: asset.id } },
          body: { work_id: id, expected_revision: asset.match_revision },
        }),
      ),
    onSuccess: async (_, workId) => {
      await cache.invalidateQueries();
      done(workId);
    },
  });
}

export default function AssetMatchForm({
  asset,
  close,
  onMatched = close,
  heading = true,
}: {
  asset: Asset;
  close: () => void;
  onMatched?: (workId: string | null) => void;
  heading?: boolean;
}) {
  const [search, setSearch] = useState(asset.title);
  const [workId, setWorkId] = useState(asset.work_ids[0] || "");
  const works = useQuery({
    queryKey: ["match-search", search],
    queryFn: async () =>
      result(
        await api.GET("/api/catalog/works", {
          params: { query: { q: search, limit: 100 } },
        }),
      ),
    enabled: search.trim().length > 1,
  });
  const match = useAssetMatch(asset, onMatched);
  return (
    <form
      className="panel editor"
      onSubmit={(event) => {
        event.preventDefault();
        match.mutate(workId);
      }}
    >
      {heading && <h2>Match {asset.title}</h2>}
      <p className="muted">
        Your correction is preserved during future syncs. Confirming a match
        also accepts the current edition or recording details.
      </p>
      <Notice error={works.error || match.error} />
      {asset.work_ids.length > 1 && (
        <p className="notice">
          This replaces all current book associations for this library item. The
          correction history can restore them.
        </p>
      )}
      <label>
        Search catalog
        <input
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
            setWorkId("");
          }}
        />
      </label>
      <label>
        Book
        <select
          value={workId}
          onChange={(event) => setWorkId(event.target.value)}
          required
        >
          <option value="">Choose the correct book</option>
          {works.data?.items.map((work) => (
            <option key={work.id} value={work.id}>
              {work.title} — {work.authors.join(", ") || "Unknown author"}
            </option>
          ))}
        </select>
      </label>
      <div className="button-row">
        <button className="primary" disabled={!workId || match.isPending}>
          Confirm match
        </button>
        <button
          type="button"
          disabled={match.isPending}
          onClick={() => match.mutate(null)}
        >
          Leave unmatched
        </button>
        <button type="button" onClick={close}>
          Cancel
        </button>
      </div>
      <IdentityHistory entityId={asset.id} onChanged={close} />
    </form>
  );
}
