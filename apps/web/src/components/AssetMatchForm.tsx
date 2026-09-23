import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Notice } from "../components";
import IdentityHistory from "../pages/IdentityHistory";
import { searchTitle, titlePart } from "../titleLabels";

type Asset = components["schemas"]["AssetView"];

type Part = [number, number] | null;

export function useAssetMatch(
  asset: Asset,
  done: (workId: string | null) => void,
) {
  const cache = useQueryClient();
  return useMutation({
    // `part` undefined keeps the part number the title gives, if any.
    mutationFn: async ({ id, part }: { id: string | null; part?: Part }) =>
      result(
        await api.POST("/api/library/assets/{asset_id}/match", {
          params: { path: { asset_id: asset.id } },
          body: {
            work_id: id,
            expected_revision: asset.match_revision,
            part_index: part ? part[0] : null,
            part_total: part ? part[1] : null,
            whole_book: part === null,
          },
        }),
      ),
    onSuccess: async (_, { id }) => {
      await cache.invalidateQueries();
      done(id);
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
  const [search, setSearch] = useState(searchTitle(asset.title));
  const [workId, setWorkId] = useState(asset.work_ids[0] || "");
  const [part, setPart] = useState<Part>(
    asset.part_index && asset.part_total
      ? [asset.part_index, asset.part_total]
      : titlePart(asset.title),
  );
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
        match.mutate({ id: workId, part });
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
      <label className="check-label">
        <input
          type="checkbox"
          checked={!!part}
          onChange={(event) => setPart(event.target.checked ? [1, 2] : null)}
        />
        This item is one part of the book
      </label>
      {part && (
        <fieldset className="part-number">
          <legend className="sr-only">Part number</legend>
          <label>
            Part
            <input
              type="number"
              min={1}
              max={part[1]}
              value={part[0]}
              onChange={(event) =>
                setPart([Number(event.target.value) || 1, part[1]])
              }
              required
            />
          </label>
          <label>
            of
            <input
              type="number"
              min={Math.max(2, part[0])}
              max={20}
              value={part[1]}
              onChange={(event) =>
                setPart([part[0], Number(event.target.value) || 2])
              }
              required
            />
          </label>
          <p className="muted">
            The book counts as in your library only when every part is.
          </p>
        </fieldset>
      )}
      <div className="button-row">
        <button className="primary" disabled={!workId || match.isPending}>
          Confirm match
        </button>
        <button
          type="button"
          disabled={match.isPending}
          onClick={() => match.mutate({ id: null })}
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
