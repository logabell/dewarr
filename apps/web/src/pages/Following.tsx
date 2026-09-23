import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import { randomUUID } from "../randomUUID";
import ListPolicy from "./ListPolicy";
import ListRequests from "./ListRequests";

type Follow = components["schemas"]["CatalogFollowView"];
type Filters = components["schemas"]["FollowFilters"];
const defaults: Filters = {
  compilations: false,
  box_sets: false,
  anthologies: false,
  non_main_series: false,
  coauthored: true,
  language: null,
};

function FilterFields({
  value,
  change,
}: {
  value: Filters;
  change: (value: Filters) => void;
}) {
  const labels = {
    compilations: "Include compilations",
    box_sets: "Include box sets",
    anthologies: "Include anthologies",
    non_main_series: "Include non-main-series titles",
    coauthored: "Include co-authored books",
  } as const;
  return (
    <fieldset>
      <legend>Books to include</legend>
      {Object.entries(labels).map(([key, label]) => (
        <label className="check-label" key={key}>
          <input
            type="checkbox"
            checked={value[key as keyof typeof labels] ?? false}
            onChange={(e) => change({ ...value, [key]: e.target.checked })}
          />
          {label}
        </label>
      ))}
      <label>
        Edition language (optional)
        <input
          value={value.language || ""}
          placeholder="en"
          pattern="[a-z]{2}"
          maxLength={2}
          onChange={(e) =>
            change({ ...value, language: e.target.value.toLowerCase() || null })
          }
        />
      </label>
      <p className="muted">
        Use a two-letter language code. Books without a matching edition are
        excluded. Collection filters use Hardcover series flags, titles and
        tags.
      </p>
    </fieldset>
  );
}

export default function Following() {
  const [params, setParams] = useSearchParams();
  const cache = useQueryClient();
  const [filters, setFilters] = useState<Filters>(defaults);
  const kind = params.get("kind");
  const externalId = params.get("externalId");
  const name = params.get("name") || "";
  const query = useQuery({
    queryKey: ["following"],
    queryFn: async () => result(await api.GET("/api/following")),
    refetchInterval: (q) =>
      q.state.data?.some((f) =>
        ["queued", "running"].includes(f.subscription.state),
      )
        ? 1500
        : 30_000,
  });
  const create = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/following", {
          body: {
            source_kind: kind as "author" | "series",
            external_id: Number(externalId),
            name: name.slice(0, 200),
            filters,
          },
        }),
      ),
    onSuccess: (value) => {
      void cache.invalidateQueries({ queryKey: ["following"] });
      setParams({ list: value.list_id });
    },
  });
  const existing = query.data?.find(
    (f) => f.source_kind === kind && f.external_id === externalId,
  );
  const current =
    query.data?.find((f) => f.list_id === params.get("list")) || existing;
  return (
    <section className="reader-page">
      <h1>Following</h1>
      <p>
        Follow authors and series. Future books only is the default; no existing
        books are requested without a reviewed selection.
      </p>
      <Notice error={query.error || create.error} />
      {query.isPending ? <Loading /> : null}
      {(kind === "author" || kind === "series") && externalId && !existing ? (
        <form
          className="panel editor"
          onSubmit={(e) => {
            e.preventDefault();
            create.mutate();
          }}
        >
          <h2>Follow {name}</h2>
          <FilterFields value={filters} change={setFilters} />
          <p>
            First we verify the complete catalog. Then choose Browse, Manual or
            Automatic, media, and a download profile. Include back catalog by
            selecting up to 25 current books in the policy preview.
          </p>
          <button className="primary" disabled={create.isPending}>
            Follow and preview catalog
          </button>
        </form>
      ) : null}
      {query.data?.length === 0 && !kind ? (
        <p>Open an author or series page and choose Follow to get started.</p>
      ) : null}
      <div className="button-row">
        {query.data?.map((f) => (
          <Link key={f.list_id} to={`/following?list=${f.list_id}`}>
            {f.name} · {f.source_kind} ·{" "}
            {f.subscription.enabled ? f.mode : "paused"}
          </Link>
        ))}
      </div>
      {current ? (
        <FollowEditor
          key={`${current.list_id}:${current.subscription.generation}`}
          follow={current}
        />
      ) : null}
    </section>
  );
}

function FollowEditor({ follow }: { follow: Follow }) {
  const cache = useQueryClient();
  const [, setParams] = useSearchParams();
  const [filters, setFilters] = useState(follow.filters);
  const [offset, setOffset] = useState(0);
  const path = { list_id: follow.list_id };
  const changed = () => {
    for (const key of [
      "following",
      "follow-observations",
      "list-policy",
      "list-policy-books",
      "list-books",
    ])
      void cache.invalidateQueries({ queryKey: [key] });
  };
  const edit = useMutation({
    mutationFn: async (enabled: boolean) =>
      result(
        await api.PATCH("/api/following/{list_id}", {
          params: { path },
          body: {
            expected_generation: follow.subscription.generation,
            enabled,
            filters,
          },
        }),
      ),
    onSuccess: changed,
  });
  const unfollow = useMutation({
    mutationFn: async () =>
      result(
        await api.DELETE("/api/following/{list_id}", { params: { path } }),
      ),
    onSuccess: () => {
      changed();
      setParams({});
    },
  });
  const sync = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/lists/{list_id}/subscription/sync", {
          params: { path, header: { "idempotency-key": randomUUID() } },
        }),
      ),
    onSuccess: changed,
  });
  const observations = useQuery({
    queryKey: [
      "follow-observations",
      follow.list_id,
      follow.subscription.last_success_at,
      offset,
    ],
    queryFn: async () =>
      result(
        await api.GET("/api/lists/{list_id}/subscription/observations", {
          params: { path, query: { offset, limit: 50 } },
        }),
      ),
  });
  const exclude = useMutation({
    mutationFn: async (book: { id: string; excluded: boolean }) =>
      result(
        await api.PATCH(
          "/api/lists/{list_id}/subscription/observations/{observation_id}",
          {
            params: { path: { ...path, observation_id: book.id } },
            body: { excluded: !book.excluded },
          },
        ),
      ),
    onSuccess: changed,
  });
  const ready =
    follow.subscription.enabled &&
    follow.subscription.completeness === "verified-observation";
  return (
    <article>
      <h2>{follow.name}</h2>
      <Link
        to={`/${follow.source_kind === "author" ? "authors" : "series"}/hardcover/${follow.external_id}`}
      >
        Open {follow.source_kind}
      </Link>
      <p role="status">{follow.subscription.message}</p>
      <p>
        {follow.subscription.observed_count} observed ·{" "}
        {follow.subscription.excluded_count} excluded by you
      </p>
      <Notice
        error={
          edit.error ||
          unfollow.error ||
          sync.error ||
          observations.error ||
          exclude.error
        }
      />
      <div className="button-row">
        <button
          disabled={edit.isPending}
          onClick={() => edit.mutate(!follow.subscription.enabled)}
        >
          {follow.subscription.enabled ? "Pause follow" : "Resume follow"}
        </button>
        <button
          disabled={sync.isPending || !follow.subscription.enabled}
          onClick={() => sync.mutate()}
        >
          Refresh catalog
        </button>
        <button disabled={unfollow.isPending} onClick={() => unfollow.mutate()}>
          Unfollow
        </button>
      </div>
      <details>
        <summary>Edit follow filters</summary>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            edit.mutate(follow.subscription.enabled);
          }}
        >
          <FilterFields value={filters} change={setFilters} />
          <p>
            Saving pauses acquisition until you review a fresh policy preview.
            Exclusions are preserved.
          </p>
          <button disabled={edit.isPending}>Save filters and refresh</button>
        </form>
      </details>
      {ready ? (
        <>
          <ListPolicy
            key={`${follow.list_id}:${follow.subscription.last_success_at}`}
            listId={follow.list_id}
            follow
          />
          {follow.mode === "manual" ? (
            <ListRequests listId={follow.list_id} />
          ) : null}
        </>
      ) : (
        <p>
          Acquisition stays paused until a complete catalog is verified and you
          activate a policy.
        </p>
      )}
      <section className="panel editor">
        <h3>Books and exclusions</h3>
        {observations.data?.items.map((book) => (
          <article className="source-attribution" key={book.id}>
            <div>
              {book.work_id ? (
                <Link to={`/books/${book.work_id}`}>{book.title}</Link>
              ) : (
                <strong>{book.title}</strong>
              )}
              <p>
                {book.excluded
                  ? "Excluded by you"
                  : book.filter_reason ||
                    (book.present ? "Included" : "No longer listed")}
              </p>
              <button
                disabled={exclude.isPending}
                onClick={() => exclude.mutate(book)}
              >
                {book.excluded ? "Remove exclusion" : "Exclude book"}
              </button>
            </div>
          </article>
        ))}
        <div className="button-row">
          {offset > 0 ? (
            <button onClick={() => setOffset(offset - 50)}>
              Previous books
            </button>
          ) : null}
          {offset + 50 < (observations.data?.total || 0) ? (
            <button onClick={() => setOffset(offset + 50)}>Next books</button>
          ) : null}
        </div>
      </section>
    </article>
  );
}
