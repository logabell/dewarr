import { usePagedQuery } from "../hooks/usePagedQuery";
import DownloadRecoveryDetails from "./DownloadRecoveryDetails";
import InfiniteScroll from "./InfiniteScroll";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
export default function BookDownloads({ workId }: { workId: string }) {
  const query = usePagedQuery({
    queryKey: ["book-downloads", workId],
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/acquisition/downloads", {
          params: { query: { work_id: workId, offset, limit: 5 } },
          signal,
        }),
      ),
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.total ? count : undefined;
    },
    refetchInterval: 15_000,
  });
  return (
    <section
      id="downloads"
      className="reader-section"
      aria-labelledby="downloads-heading"
    >
      <div className="section-heading">
        <div>
          <p className="eyebrow">FROM SOURCE TO SHELF</p>
          <h2 id="downloads-heading">Your download history</h2>
        </div>
        <Link to="/requests?status=downloading">View download queue →</Link>
      </div>
      <Notice error={query.error} />
      {query.isPending && <Loading />}
      {query.error && (
        <button onClick={() => query.refetch()}>Retry download history</button>
      )}
      {query.data && (
        <>
          {!query.data.items.length ? (
            <p className="muted">
              No downloads recorded for this book in your account. Copies added
              outside this app may not have a download source recorded.
            </p>
          ) : (
            <div className="book-table-scroll">
              <table className="book-data-table">
                <thead>
                  <tr>
                    <th>Release</th>
                    <th>Source</th>
                    <th>Date</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {query.data.items.map((item) => (
                    <tr key={item.id}>
                      <td>
                        <strong>{item.release_title}</strong>
                        <small>{item.message}</small>
                        <DownloadRecoveryDetails
                          attemptId={item.id}
                          workId={workId}
                        />
                        {item.progress != null && item.progress < 1 && (
                          <progress
                            max={1}
                            value={item.progress}
                            aria-label="Download progress"
                          />
                        )}
                      </td>
                      <td>
                        {(
                          {
                            mam: "MyAnonamouse",
                            prowlarr: "Prowlarr",
                            audiobookbay: "AudiobookBay",
                          } as Record<string, string>
                        )[item.source || ""] ||
                          item.source ||
                          "Unknown"}
                      </td>
                      <td>
                        <time dateTime={item.created_at}>
                          {new Date(item.created_at).toLocaleDateString()}
                        </time>
                      </td>
                      <td>
                        <span className="status">
                          {item.state.replaceAll("-", " ")}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <InfiniteScroll query={query} />
        </>
      )}
    </section>
  );
}
