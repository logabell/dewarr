import { lazy, Suspense } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Loading } from "../components";
import { useRequestCounts } from "../hooks/useRequestCounts";
import { requestFilter, type RequestFilter } from "./requestFilters";

const SavedRequests = lazy(() => import("./ActivityRequests"));

const filters: {
  id: RequestFilter;
  title: string;
  approve?: boolean;
  admin?: boolean;
}[] = [
  { id: "all", title: "All" },
  { id: "pending", title: "Pending", approve: true },
  { id: "downloading", title: "Downloading" },
  { id: "library", title: "In library" },
  { id: "declined", title: "Declined" },
  { id: "withdrawn", title: "Withdrawn" },
  { id: "review", title: "Review", admin: true },
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

  return (
    <div className="requests-page">
      <h1 className="sr-only">Requests</h1>
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
          <Link className="page-view-action" to="/discover">
            Find books
          </Link>
        </div>
      </div>
      <Suspense fallback={<Loading />}>
        <SavedRequests canManage={canRequest} status={status} sort={sort} />
      </Suspense>
    </div>
  );
}
