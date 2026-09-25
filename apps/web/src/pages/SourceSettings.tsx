import ConnectionStatus from "../components/ConnectionStatus";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
import { MamConnectionForm } from "./Sources";
import { AudiobookBayConnectionForm } from "./AudiobookBaySources";
import { ProwlarrConnectionForm } from "./ProwlarrSources";
import { SlskdConnectionForm } from "./SlskdSettings";

export default function SourceSettings() {
  const cache = useQueryClient();
  const mam = useQuery({
    refetchInterval: 30_000,
    queryKey: ["mam-connection"],
    queryFn: async () => result(await api.GET("/api/sources/mam/connection")),
  });
  const abb = useQuery({
    refetchInterval: 30_000,
    queryKey: ["abb-connection"],
    queryFn: async () =>
      result(await api.GET("/api/sources/audiobookbay/connection")),
  });
  const slskd = useQuery({
    refetchInterval: 30_000,
    queryKey: ["slskd-connection"],
    queryFn: async () => result(await api.GET("/api/sources/slskd/connection")),
  });
  const prowlarr = useQuery({
    refetchInterval: 30_000,
    queryKey: ["prowlarr-connection"],
    queryFn: async () =>
      result(await api.GET("/api/sources/prowlarr/connection")),
  });
  return (
    <div className="source-settings">
      <section aria-label="MAM settings">
        <details className="source-connection">
          <summary>
            <span>MAM</span>
            <ConnectionStatus
              status={mam.isPending ? "checking" : mam.data?.status}
            />
          </summary>
          <Notice error={mam.error} />
          {mam.isPending && <Loading />}
          {mam.data && (
            <MamConnectionForm
              key={mam.data.configured ? "connected" : "disconnected"}
              value={mam.data}
            />
          )}
        </details>
      </section>
      <section aria-label="Prowlarr settings">
        <details className="source-connection">
          <summary>
            <span>Prowlarr</span>
            <ConnectionStatus
              status={prowlarr.isPending ? "checking" : prowlarr.data?.status}
            />
          </summary>
          <Notice error={prowlarr.error} />
          {prowlarr.isPending && <Loading />}
          {prowlarr.data && (
            <ProwlarrConnectionForm
              key={prowlarr.data.generation}
              connection={prowlarr.data}
              onSaved={() => {
                cache.invalidateQueries({ queryKey: ["prowlarr-connection"] });
                cache.invalidateQueries({ queryKey: ["prowlarr-indexers"] });
              }}
            />
          )}
        </details>
      </section>
      <section aria-label="Soulseek settings">
        <details className="source-connection">
          <summary>
            <span>Soulseek</span>
            <ConnectionStatus
              status={slskd.isPending ? "checking" : slskd.data?.status}
            />
          </summary>
          <Notice error={slskd.error} />
          {slskd.isPending && <Loading />}
          {slskd.data && (
            <SlskdConnectionForm
              key={slskd.data.configured ? "connected" : "disconnected"}
              value={slskd.data}
            />
          )}
        </details>
      </section>
      <section aria-label="AudiobookBay settings">
        <details className="source-connection">
          <summary>
            <span>AudiobookBay</span>
            <ConnectionStatus
              status={abb.isPending ? "checking" : abb.data?.status}
            />
          </summary>
          <Notice error={abb.error} />
          {abb.isPending && <Loading />}
          {abb.data && (
            <AudiobookBayConnectionForm
              key={abb.data.generation}
              value={abb.data}
            />
          )}
        </details>
      </section>
    </div>
  );
}
