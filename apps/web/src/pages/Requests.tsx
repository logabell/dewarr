import { lazy, Suspense } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import ContentSkeleton from "../components/ContentSkeleton";
import { useRequestCounts } from "../hooks/useRequestCounts";
import { requestFilter, type RequestFilter } from "./requestFilters";

const SavedRequests = lazy(() => import("./ActivityRequests"));

const filters: {
  id: RequestFilter;
  title: string;
  approve?: boolean;
  admin?: boolean;
}[] = [
  { id: "all", title: "Active" },
  { id: "pending", title: "Pending", approve: true },
  { id: "downloading", title: "Downloading" },
  { id: "library", title: "In library" },
  { id: "declined", title: "Declined" },
  { id: "withdrawn", title: "Withdrawn" },
  { id: "review", title: "Download review", admin: true },
];

export default function Requests({
  admin,
  canRequest,
  canApprove,
}: {
  admin: boolean;
  canRequest: boolean;
  canApprove: boolean;
}) {
  const { hash, search } = useLocation();
  const navigate = useNavigate();
  const params = new URLSearchParams(search);
  const selected = requestFilter(hash, params.get("status"));
  const sort = params.get("sort") === "title" ? "title" : "newest";
  const counts = useRequestCounts().data;
  const visible = filters.filter(
    (filter) => (!filter.approve || canApprove) && (!filter.admin || admin),
  );
  const status =
    (selected === "review" && !admin) || (selected === "pending" && !canApprove)
      ? "all"
      : selected;

  function queryFor(nextStatus: RequestFilter, nextSort = sort) {
    const next = new URLSearchParams(search);
    if (nextStatus === "all") next.delete("status");
    else next.set("status", nextStatus);
    if (nextSort === "newest") next.delete("sort");
    else next.set("sort", nextSort);
    const query = next.toString();
    return query ? `/requests?${query}` : "/requests";
  }

  const cleared = new URLSearchParams(search);
  cleared.delete("q");
  return (
    <div className="requests-page">
      <h1 className="workspace-title">Requests</h1>
      <div className="page-tabs-bar">
        <nav className="page-tabs" aria-label="Request filters">
          {visible.map((filter) => (
            <Link
              key={filter.id}
              aria-label={filter.title}
              to={queryFor(filter.id)}
              aria-current={status === filter.id ? "page" : undefined}
            >
              {filter.title}
              {(["pending", "downloading", "review"] as string[]).includes(
                filter.id,
              ) &&
                (counts?.[filter.id as "pending" | "downloading" | "review"] ??
                  0) > 0 && (
                  <span className="requests-tab-count">
                    {
                      counts?.[
                        filter.id as "pending" | "downloading" | "review"
                      ]
                    }
                  </span>
                )}
            </Link>
          ))}
        </nav>
        <div className="page-tabs-tools">
          <label className="request-sort">
            <span className="sr-only">Sort</span>
            <select
              aria-label="Sort requests"
              value={sort}
              onChange={(event) =>
                navigate(
                  queryFor(
                    status,
                    event.target.value === "title" ? "title" : "newest",
                  ),
                )
              }
            >
              <option value="newest">Newest</option>
              <option value="title">Title</option>
            </select>
          </label>
          {admin && (
            <Link className="button-link" to="/organization/inspections">
              Import local files
            </Link>
          )}
          <Link className="page-view-action" to="/discover">
            Find books
          </Link>
        </div>
      </div>
      <form
        className="request-search"
        role="search"
        onSubmit={(event) => {
          event.preventDefault();
          const next = new URLSearchParams(search);
          const q = String(
            new FormData(event.currentTarget).get("q") || "",
          ).trim();
          if (q) next.set("q", q);
          else next.delete("q");
          navigate(`/requests?${next}`);
        }}
      >
        <input
          type="search"
          name="q"
          aria-label="Search requests"
          placeholder="Search requests by title or author…"
          defaultValue={params.get("q") || ""}
          key={params.get("q") || ""}
          maxLength={200}
        />
        <button>Search requests</button>
        {params.get("q") && (
          <Link to={`/requests?${cleared}`}>Clear search</Link>
        )}
      </form>
      <Suspense fallback={<ContentSkeleton />}>
        <SavedRequests
          canManage={canRequest}
          status={status}
          sort={sort}
          q={params.get("q") || ""}
        />
      </Suspense>
    </div>
  );
}
