import { useQuery } from "@tanstack/react-query";
import { api, result } from "../api/client";

/** Review totals and reasons; the nav badge reads `total`. */
export function useLibraryReviewCount(enabled: boolean) {
  return useQuery({
    queryKey: ["library-review", "summary"],
    queryFn: async () => result(await api.GET("/api/library/review/summary")),
    enabled,
    refetchInterval: 60_000,
  });
}
