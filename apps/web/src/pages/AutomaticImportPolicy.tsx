import SettingHelp from "../components/SettingHelp";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import { Notice } from "../components";
import { Link } from "react-router-dom";

export default function AutomaticImportPolicy({
  destinationId,
  revision,
  verified,
  unsaved,
  onVerify,
}: {
  destinationId: string;
  revision: string;
  verified: boolean;
  unsaved: boolean;
  onVerify: () => void;
}) {
  const cache = useQueryClient();
  const query = useQuery({
    queryKey: ["automatic-import-policy", destinationId, revision, verified],
    queryFn: async () =>
      result(
        await api.GET(
          "/api/organization/destinations/{destination_id}/automatic-import",
          {
            params: { path: { destination_id: destinationId } },
          },
        ),
      ),
  });
  const save = useMutation({
    mutationFn: async (enabled: boolean) =>
      result(
        await api.PUT(
          "/api/organization/destinations/{destination_id}/automatic-import",
          {
            params: { path: { destination_id: destinationId } },
            body: {
              enabled,
              defer_until_verified: enabled && !query.data!.can_enable,
              expected_generation: query.data!.generation,
              destination_revision: revision,
            },
          },
        ),
      ),
    onSettled: () =>
      cache.invalidateQueries({
        queryKey: ["automatic-import-policy", destinationId],
      }),
  });
  const requested =
    query.data?.requested_enabled ??
    (query.data?.generation ? query.data.enabled : true);
  return (
    <section
      aria-label="Automatic import policy"
      className="automatic-import-setting"
    >
      <div className="setting-subheading">
        <h3>Import on completion</h3>
        <SettingHelp label="automatic import">
          Applies to new downloads after enabling. EPUB and identified M4B/MP3
          recordings import when identity and completeness checks pass.
          Ambiguous books, unsupported formats and incomplete tracks stay for
          review. Disabling holds unpublished automatic work and preserves
          published files.
        </SettingHelp>
      </div>
      <Notice error={query.error || save.error} />
      {query.data && (
        <>
          <p role="status">
            {query.data.ready
              ? "On · matched downloads import automatically when complete."
              : requested && !verified
                ? "On after verification · matched downloads will import automatically once setup is complete."
                : requested
                  ? query.data.message
                  : "Off · completed downloads stay in file review."}
          </p>
          <div className="button-row">
            {!requested && (
              <button
                disabled={unsaved || save.isPending}
                onClick={() => save.mutate(true)}
              >
                {query.data.can_enable
                  ? "Enable automatic import"
                  : "Turn on after verification"}
              </button>
            )}
            {requested && !query.data.ready && query.data.can_enable && (
              <button
                disabled={unsaved || save.isPending}
                onClick={() => save.mutate(true)}
              >
                Enable automatic import
              </button>
            )}
            {requested && (
              <button
                disabled={unsaved || save.isPending}
                onClick={() => save.mutate(false)}
              >
                Disable automatic import
              </button>
            )}
          </div>
          {!query.data.ready && verified && !query.data.can_enable && (
            <p className="muted">
              <Link to="/settings#naming">Review naming settings</Link> or{" "}
              <button type="button" onClick={onVerify}>
                Verify folder again
              </button>
              .
            </p>
          )}
        </>
      )}
    </section>
  );
}
