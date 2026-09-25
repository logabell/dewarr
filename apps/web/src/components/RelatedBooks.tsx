import { useQuery } from "@tanstack/react-query";
import { browseCache, refreshPending } from "../queryPolicies";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
import DiscoveryShelf from "./DiscoveryShelf";

export default function RelatedBooks({ workId }: { workId: string }) {
  const query = useQuery({
    queryKey: ["discovery", "related", workId],
    ...browseCache,
    staleTime: 60_000,
    queryFn: async () =>
      result(
        await api.GET("/api/discovery/related/{work_id}", {
          params: { path: { work_id: workId } },
        }),
      ),
    refetchInterval: (query) =>
      query.state.status !== "error" && query.state.data?.retry_after
        ? Math.max(60_000, query.state.data.retry_after * 1_000)
        : refreshPending(query),
    retry: false,
  });
  return (
    <section className="discovery-section" aria-label="Related books">
      <Notice error={query.error} />
      {query.isPending && <Loading />}
      {query.data && <DiscoveryShelf shelf={query.data} />}
    </section>
  );
}
