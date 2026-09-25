import { useQuery } from "@tanstack/react-query";
import { api, result } from "../api/client";

export function useRequestCounts() {
  return useQuery({
    queryKey: ["requests", "counts"],
    queryFn: async ({ signal }) =>
      result(await api.GET("/api/requests/counts", { signal })),
    refetchInterval: 5000,
    staleTime: 3000,
  });
}
