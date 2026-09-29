import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import { Link } from "react-router-dom";
import { randomUUID } from "../randomUUID";

type Inspection = components["schemas"]["InspectionView"];
type Plan = components["schemas"]["FrozenPlanView"];

/** A saved download needs a decision about its files, not another catalog workflow. */
export default function DownloadMatchReview({
  inspection,
  retry,
  retrying,
}: {
  inspection: Inspection;
  retry: () => void;
  retrying: boolean;
}) {
  const download = inspection.download!;
  const cache = useQueryClient();
  const [destinationId, setDestinationId] = useState("");
  const [complete, setComplete] = useState(false);
  const [reviewingFiles, setReviewingFiles] = useState(false);
  const prepared = useRef<{
    plan: Plan;
    destination: { id: string; revision: string };
    key: string;
  } | null>(null);
  const context = useQuery({
    retry: (attempt, error) =>
      attempt < 1 && error instanceof ApiError && error.status === 409,
    queryKey: [
      "download-review-context",
      inspection.id,
      inspection.snapshot?.revision,
    ],
    queryFn: async () => {
      const [grouping, settings, destinations] = await Promise.all([
        api
          .GET("/api/organization/inspections/{inspection_id}/grouping", {
            params: { path: { inspection_id: inspection.id } },
          })
          .then(result),
        api.GET("/api/organization/settings").then(result),
        api.GET("/api/organization/destinations").then(result),
      ]);
      const matches = result(
        await api.GET("/api/organization/inspections/{inspection_id}/matches", {
          params: {
            path: { inspection_id: inspection.id },
            query: { grouping_revision: grouping.revision, limit: 10 },
          },
        }),
      );
      return { grouping, settings, destinations, matches };
    },
  });
  const data = context.data;
  const group =
    data?.grouping.content.groups.length === 1
      ? data.grouping.content.groups[0]
      : undefined;
  const match = data?.matches.items.find(
    (item) => item.group_key === group?.key,
  );
  const candidates =
    match?.candidates.filter((item) => item.work_id === download.work_id) || [];
  const identified = candidates.filter((item) => item.identifier_match);
  const candidate =
    candidates.find((item) => item.version_id === match?.selected_version_id) ||
    (identified.length === 1 ? identified[0] : undefined);
  const issues = [
    ...(download.file_conflicts || []),
    ...(match?.evidence.issues || []),
  ];
  const conflicts =
    candidate?.conflicts.filter(
      (reason) =>
        !(
          reason === "Embedded title is missing or differs" &&
          !match?.evidence.titles?.length
        ) &&
        !(
          reason === "Embedded authors are missing or differ" &&
          !match?.evidence.authors?.length
        ),
    ) || [];
  const blocked =
    !group ||
    group.medium !== download.medium ||
    !match ||
    match.truncated ||
    identified.length > 1 ||
    conflicts.length > 0 ||
    issues.length > 0 ||
    match.candidates.some(
      (item) => item.identifier_match && item.work_id !== download.work_id,
    );
  const unreadable =
    inspection.snapshot?.files.filter(
      (file) =>
        file.state === "held" &&
        (file.medium ||
          /\.(epub|pdf|cbz|m4b|m4a|mp3|flac|ogg|opus|aac|wav|wma)$/i.test(
            file.path,
          )),
    ) || [];
  const reason = unreadable.length
    ? unreadable[0].reason ||
      "The downloaded book could not be read. Check its files and try again."
    : data?.grouping.content.groups.length === 0
      ? "No supported book files were found in this download."
      : !group
        ? "These files may contain several books. Choose a download containing only the requested book."
        : identified.length > 1
          ? "The files point to more than one catalog edition. We need a clearer release to avoid importing the wrong edition."
          : download.file_conflicts?.length
            ? download.file_conflicts[0]
            : issues.length
              ? "Some files could not be verified or contain conflicting information."
              : "The downloaded files do not agree with the book you requested.";
  const destinations =
    data?.destinations.filter(
      (item) =>
        item.medium === download.medium &&
        item.enabled &&
        item.publication_available,
    ) || [];
  const saved = destinations.filter(
    (item) =>
      item.backend_path === download.destination ||
      item.local_path === download.destination,
  );
  const needsDestination =
    destinations.length > 1 && !download.destination_id && saved.length !== 1;
  const destination =
    destinations.find((item) => item.id === destinationId) ||
    (download.destination_id
      ? destinations.find((item) => item.id === download.destination_id)
      : saved.length === 1
        ? saved[0]
        : destinations.length === 1
          ? destinations[0]
          : undefined);
  const confirm = useMutation({
    mutationFn: async () => {
      if (!prepared.current) {
        if (!data || !group || !destination || blocked || !complete)
          throw new Error("Refresh the review before importing.");
        const version =
          candidate?.version_id ||
          result(
            await api.POST(
              "/api/organization/inspections/{inspection_id}/editions",
              {
                params: { path: { inspection_id: inspection.id } },
                body: {
                  work_id: download.work_id,
                  group_key: group.key,
                  grouping_revision: data.grouping.revision,
                },
              },
            ),
          ).version_id;
        const plan = result(
          await api.POST(
            "/api/organization/inspections/{inspection_id}/plans",
            {
              params: { path: { inspection_id: inspection.id } },
              body: {
                inspection_revision: inspection.snapshot!.revision,
                grouping_revision: data.grouping.revision,
                profile_revision: data.settings.revision,
                include_covers: true,
                destinations: { [download.medium]: destination.id },
                selections: [
                  {
                    group_key: group.key,
                    work_id: download.work_id,
                    version_id: version,
                    full_content: true,
                    contents_confirmed: false,
                    ...(match?.status === "matched"
                      ? { match_revision: match.revision }
                      : {}),
                  },
                ],
              },
            },
          ),
        );
        prepared.current = {
          plan,
          destination: { id: destination.id, revision: destination.revision },
          key: randomUUID(),
        };
      }
      const frozen = prepared.current;
      return result(
        await api.POST("/api/organization/plans/{plan_id}/imports", {
          params: {
            path: { plan_id: frozen.plan.id },
            header: { "idempotency-key": frozen.key },
          },
          body: {
            plan_revision: frozen.plan.revision,
            destinations: { [download.medium]: frozen.destination },
          },
        }),
      );
    },
    onSuccess: async () => {
      await Promise.all([
        cache.invalidateQueries({ queryKey: ["inspection", inspection.id] }),
        cache.invalidateQueries({ queryKey: ["requests"] }),
      ]);
    },
  });
  return (
    <section className="download-attention" aria-label="Download next step">
      {context.isPending ? (
        <Loading />
      ) : data ? (
        blocked ? (
          <>
            <h3>This download needs a closer look</h3>
            <p>{reason}</p>
            {!!match?.evidence.titles?.length && (
              <p className="download-file-title">
                <span>Title found in the files</span>
                <strong>{match.evidence.titles.join(" · ")}</strong>
              </p>
            )}
            <div className="actions">
              {!!unreadable.length && download.can_retry && (
                <button className="primary" disabled={retrying} onClick={retry}>
                  {retrying ? "Checking files…" : "Check files again"}
                </button>
              )}
              <Link
                className="button"
                to={`/books/${download.work_id}?tab=sources`}
              >
                Find another download
              </Link>
              <Link to={`/books/${download.work_id}?tab=manage`}>
                Review book metadata
              </Link>
            </div>
          </>
        ) : download.can_retry && !reviewingFiles ? (
          <>
            <h3>Finish checking the download</h3>
            <p role="status">
              {download.message ||
                "Check the completed files again and add them to your library."}
            </p>
            <div className="actions">
              <button className="primary" disabled={retrying} onClick={retry}>
                {retrying ? "Checking download…" : "Continue automatically"}
              </button>
              <button onClick={() => setReviewingFiles(true)}>
                Confirm completeness myself
              </button>
            </div>
          </>
        ) : (
          <>
            <h3>Do these files contain the whole book?</h3>
            <p>
              Confirm that every chapter is included. The selected book and
              library folder are already saved.
            </p>
            <label className="download-complete-check">
              <input
                type="checkbox"
                checked={complete}
                disabled={confirm.isPending || !!prepared.current}
                onChange={(event) => setComplete(event.target.checked)}
              />
              <span>These files contain the complete book</span>
            </label>
            {needsDestination && (
              <label className="download-destination">
                Library folder
                <select
                  value={destinationId}
                  onChange={(event) => setDestinationId(event.target.value)}
                >
                  <option value="">Choose a library folder</option>
                  {destinations.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.backend_path}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {(!destinations.length ||
              (download.destination_id && !destination)) && (
              <p>
                Connect a library folder in{" "}
                <Link to="/settings#naming">Library settings</Link> to continue.
              </p>
            )}
            <div className="actions">
              <button
                className="primary"
                disabled={!destination || !complete || confirm.isPending}
                onClick={() => confirm.mutate()}
              >
                {confirm.isPending
                  ? "Adding to library…"
                  : prepared.current
                    ? "Retry import"
                    : "Add to library"}
              </button>
              <Link to={`/books/${download.work_id}?tab=sources`}>
                Find another download
              </Link>
            </div>
          </>
        )
      ) : null}
      <Notice error={context.error || confirm.error} />
      {context.isError && (
        <button
          disabled={context.isFetching}
          onClick={() => void context.refetch()}
        >
          {context.isFetching ? "Refreshing…" : "Refresh review"}
        </button>
      )}
    </section>
  );
}
