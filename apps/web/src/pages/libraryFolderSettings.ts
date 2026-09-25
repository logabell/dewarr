import { useQuery } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import type { Medium } from "./namingBuilder";

type Destination = components["schemas"]["DestinationView"];
export function selectLibraryDestination(
  destinations: Destination[],
  id: unknown,
  medium: Medium,
) {
  const matching = destinations.filter((item) => item.medium === medium);
  return (
    matching.find((item) => item.id === id) ||
    matching.find((item) => item.root_key === `library-${medium}`) ||
    (matching.length === 1 ? matching[0] : undefined)
  );
}

export function useLibraryFolderSettings() {
  return useQuery({
    queryKey: ["library-folder-settings"],
    refetchInterval: 5000,
    queryFn: async () => {
      const [destinations, libraries, defaults] = await Promise.all([
        api.GET("/api/organization/destinations").then(result),
        api.GET("/api/library/libraries").then(result),
        api
          .GET("/api/acquisition/preferences/{scope}", {
            params: { path: { scope: "installation" } },
          })
          .then(result),
      ]);
      return { destinations, libraries, defaults };
    },
  });
}
