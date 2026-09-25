import { useMemo } from "react";
import {
  useInfiniteQuery,
  type InfiniteData,
  type Query,
  type QueryKey,
} from "@tanstack/react-query";

/** Keep loaded results while fetching the next page; filters belong in queryKey. */
export function usePagedQuery<T extends { items?: unknown[] }>({
  queryKey,
  queryFn,
  next,
  initial = 1,
  ...options
}: {
  queryKey: QueryKey;
  queryFn: (page: number, signal: AbortSignal) => Promise<T>;
  next: (
    page: NoInfer<T>,
    pages: NoInfer<T>[],
    last: number,
  ) => number | undefined;
  initial?: number;
  enabled?: boolean;
  staleTime?: number;
  gcTime?: number;
  retry?: false;
  refetchInterval?:
    | number
    | false
    | ((
        query: Query<T, Error, InfiniteData<T, number>, QueryKey>,
      ) => number | false);
}) {
  const query = useInfiniteQuery<
    T,
    Error,
    InfiniteData<T, number>,
    QueryKey,
    number
  >({
    ...options,
    queryKey: [...queryKey, "infinite"],
    initialPageParam: initial,
    queryFn: ({ pageParam, signal }) => queryFn(pageParam, signal),
    getNextPageParam: next,
  });
  const data = useMemo(() => {
    const first = query.data?.pages[0];
    return first
      ? ({
          ...first,
          items: query.data!.pages.flatMap((page) => page.items || []),
        } as T)
      : undefined;
  }, [query.data]);
  return {
    ...query,
    loadedPages: query.data?.pages,
    data,
  };
}
