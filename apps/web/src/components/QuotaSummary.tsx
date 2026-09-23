import { useQuery } from "@tanstack/react-query";
import { api, result } from "../api/client";
import { Notice } from "../components";

export default function QuotaSummary() {
  const query = useQuery({
    queryKey: ["request-quotas", "me"],
    queryFn: async () => result(await api.GET("/api/request-quotas/me")),
    refetchInterval: 15000,
  });
  const usage = query.data;
  if (query.error) return <Notice error={query.error} />;
  if (
    !usage ||
    usage.bypass ||
    (!usage.windows.length && usage.pending_remaining === null)
  )
    return null;
  return (
    <aside aria-label="Your remaining request quota" className="muted">
      {usage.windows.map((window) => (
        <p key={`${window.medium}:${window.window}`}>
          {window.medium === "audio"
            ? "Audiobooks"
            : window.medium === "ebook"
              ? "Ebooks"
              : "All media"}
          :{" "}
          {window.remaining_books === null
            ? "Unlimited books"
            : `${window.remaining_books} books remaining`}{" "}
          per rolling {window.window === "month" ? "30 days" : window.window}
          {window.remaining_bytes !== null &&
            ` · ${(window.remaining_bytes / 1024 ** 3).toFixed(2)} GiB remaining`}
          {window.capacity_returns_at &&
            ` · Capacity returns ${new Date(window.capacity_returns_at).toLocaleString()}`}
        </p>
      ))}
      {usage.pending_remaining !== null && (
        <p>{usage.pending_remaining} pending approval slots remaining.</p>
      )}
    </aside>
  );
}
