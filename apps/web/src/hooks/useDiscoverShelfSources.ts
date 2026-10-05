import { useQueries, useQuery } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { Collection } from "../components/DiscoveryCollections";
import type { components } from "../api/schema";

export function useDiscoverShelfSources(
  all = false,
  selected: string[] = [],
  known: Collection[] = [],
) {
  const collectionIds = selected.filter(
    (id) =>
      !id.startsWith("personal:") &&
      !["personal", "library", "trending", "new-releases", "upcoming"].includes(
        id,
      ) &&
      !known.some((collection) => collection.id === id),
  );
  const collections = useQuery<Collection[]>({
    queryKey: ["discover-shelf-collections", all, collectionIds],
    enabled: all || collectionIds.length > 0,
    staleTime: 300_000,
    placeholderData: (previous) => previous,
    queryFn: async ({ signal }) => {
      const items: Collection[] = [];
      for (let page = 1; ; page++) {
        const value = result(
          await api.GET("/api/discovery/collections", {
            params: {
              query: { page, limit: 100, ids: all ? undefined : collectionIds },
            },
            signal,
          }),
        );
        items.push(...value.items);
        if (!value.items.length || items.length >= value.total) return items;
      }
    },
  });
  const lists = useQuery<components["schemas"]["ListView"][]>({
    queryKey: ["lists", "discover-shelves", all],
    staleTime: 300_000,
    placeholderData: (previous) => previous,
    queryFn: async ({ signal }) => {
      const items: components["schemas"]["ListView"][] = [];
      for (let offset = 0; ; offset += 100) {
        const value = result(
          await api.GET("/api/lists/page", {
            params: { query: { offset, limit: 100 } },
            signal,
          }),
        );
        items.push(...value.items);
        if (!all || !value.items.length || items.length >= value.total)
          return items;
      }
    },
  });
  // Explicitly selected shelves remain visible even beyond the initial list page.
  const extra = useQueries({
    queries: selected
      .filter(
        (id) =>
          id.startsWith("personal:") &&
          !lists.data?.some((list) => `personal:${list.id}` === id),
      )
      .map((id) => ({
        queryKey: ["list", id.slice(9)],
        enabled: lists.isSuccess,
        staleTime: 300_000,
        queryFn: async ({ signal }: { signal: AbortSignal }) =>
          result(
            await api.GET("/api/lists/{list_id}", {
              params: { path: { list_id: id.slice(9) } },
              signal,
            }),
          ),
      })),
  });
  return {
    collections: {
      ...collections,
      data: all
        ? collections.data
        : [
            ...known,
            ...(collections.data || []).filter(
              (c) => !known.some((k) => k.id === c.id),
            ),
          ],
    },
    lists: {
      ...lists,
      data: [
        ...(lists.data || []),
        ...extra.flatMap((query) => (query.data ? [query.data] : [])),
      ],
      error: lists.error || extra.find((query) => query.error)?.error,
    },
  };
}
