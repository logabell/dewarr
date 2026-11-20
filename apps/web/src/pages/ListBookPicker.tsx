import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "../components/InfiniteScroll";
import { useEffect, useState } from "react";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";

export default function ListBookPicker({
  listId,
  selected,
  onChange,
  maximum,
  label,
  onValidityChange,
  onRevisionChange,
}: {
  listId: string;
  selected: string[];
  onChange: (ids: string[]) => void;
  maximum: number;
  label: string;
  onValidityChange: (valid: boolean) => void;
  onRevisionChange: (revision: string | undefined) => void;
}) {
  const [search, setSearch] = useState("");
  const [revision, setRevision] = useState<string>();
  const books = usePagedQuery({
    queryKey: ["list-books", listId, search, revision],
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/lists/{list_id}", {
          signal,
          params: {
            path: { list_id: listId },
            query: {
              q: search,
              offset,
              limit: 25,
              expected_revision: revision,
            },
          },
        }),
      ),
    staleTime: 0,
    gcTime: 0,
    refetchInterval: 15_000,
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.matched ? count : undefined;
    },
  });
  const valid = books.isSuccess && !books.isFetching;
  useEffect(() => {
    onValidityChange(valid);
  }, [valid, onValidityChange]);
  useEffect(() => {
    if (!revision && books.data?.content_revision) {
      setRevision(books.data.content_revision);
    }
  }, [revision, books.data?.content_revision]);
  useEffect(() => {
    onRevisionChange(revision);
  }, [revision, onRevisionChange]);
  const combined = [
    ...new Set([...selected, ...(books.data?.items.map((w) => w.id) || [])]),
  ];
  const change = (ids: string[]) => {
    if (!revision && ids.length) setRevision(books.data?.content_revision);
    if (!ids.length) setRevision(undefined);
    onChange(ids);
  };
  return (
    <section aria-label={label}>
      <label>
        {label}
        <input
          value={search}
          onChange={(event) => {
            setSearch(event.target.value);
          }}
        />
      </label>
      <p>
        {selected.length} selected · {books.data?.matched ?? "…"} matching books
      </p>
      <div className="button-row">
        <button
          type="button"
          disabled={
            !valid || !books.data?.items.length || combined.length > maximum
          }
          onClick={() => change(combined)}
        >
          Select loaded books
        </button>
        <button
          type="button"
          disabled={!selected.length}
          onClick={() => change([])}
        >
          Clear selection
        </button>
      </div>
      <Notice error={books.error} />
      {books.error && (
        <button
          type="button"
          onClick={() => {
            change([]);
            void books.refetch();
          }}
        >
          Refresh books and clear selection
        </button>
      )}
      {books.isPending && <Loading />}
      {!books.error && books.data && (
        <>
          {books.data.items.map((work) => (
            <label className="check-label" key={work.id}>
              <input
                type="checkbox"
                checked={selected.includes(work.id)}
                disabled={
                  !valid ||
                  (!selected.includes(work.id) && selected.length >= maximum)
                }
                onChange={(event) =>
                  change(
                    event.target.checked
                      ? [...selected, work.id]
                      : selected.filter((id) => id !== work.id),
                  )
                }
              />
              {work.title} {work.availability.owned ? "· In library" : ""}
            </label>
          ))}
          {!books.data.items.length && <p>No matching books in this list.</p>}
          <InfiniteScroll query={books} />
        </>
      )}
    </section>
  );
}
