import { useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { CheckCircle2, ExternalLink } from "lucide-react";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Empty, Loading, Notice } from "../components";
import AssetMatchForm, { useAssetMatch } from "../components/AssetMatchForm";
import BookDialog from "../components/BookDialog";
import InfiniteScroll from "../components/InfiniteScroll";
import { useLibraryReviewCount } from "../hooks/useLibraryReviewCount";
import { usePagedQuery } from "../hooks/usePagedQuery";
import ProviderSearch from "./ProviderSearch";

type Asset = components["schemas"]["AssetView"];
type ReadIssue = components["schemas"]["ReadIssueView"];
type Item = components["schemas"]["ReviewItem"];
type Summary = components["schemas"]["ReviewSummary"];
type Kind = "all" | "needs-matching" | "read-issue";

const KINDS: {
  id: Kind;
  label: string;
  count: (summary: Summary) => number;
}[] = [
  { id: "all", label: "Everything", count: (s) => s.total },
  {
    id: "needs-matching",
    label: "Needs matching",
    count: (s) => s.needs_matching,
  },
  {
    id: "read-issue",
    label: "Couldn't read fully",
    count: (s) => s.read_issues,
  },
];

const SOURCES = [
  ["dewarr", "In your catalog"],
  ["hardcover", "On Hardcover"],
] as const;

const REASONS: Record<string, string> = {
  title: "Missing title",
  authors: "Unreadable authors",
  narrators: "Unreadable narrators",
  language: "Unrecognized language",
  description: "Unreadable description",
  year: "Unreadable publication year",
  abridged: "Unreadable abridged flag",
  series: "Unreadable series",
  isbn: "Unreadable ISBN",
  asin: "Unreadable ASIN",
  old_id: "Unreadable legacy item ID",
};

const reasonLabel = (reason: string) => REASONS[reason] || reason;

const appName = (kind: string) =>
  kind === "grimmory" ? "Grimmory" : "Audiobookshelf";

const itemKey = (item: Item) =>
  item.kind === "asset"
    ? `asset:${item.asset!.id}`
    : `issue:${item.read_issue!.id}`;

const itemTitle = (item: Item) =>
  item.asset ? item.asset.title : item.read_issue!.title;

export default function LibraryReview() {
  const [params, setParams] = useSearchParams();
  const rawKind = params.get("kind");
  const kind: Kind =
    rawKind === "needs-matching" || rawKind === "read-issue" ? rawKind : "all";
  const q = (params.get("q") || "").slice(0, 300);
  const selectedKey = params.get("item");
  const [input, setInput] = useState(q);
  const [resolved, setResolved] = useState("");
  const update = (values: Record<string, string>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(values)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    setParams(next);
  };
  const summary = useLibraryReviewCount(true);
  const items = usePagedQuery({
    queryKey: ["library-review", kind, q],
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/library/review", {
          signal,
          params: { query: { kind, q, offset, limit: 40 } },
        }),
      ),
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.total ? count : undefined;
    },
    staleTime: 0,
    retry: false,
  });
  const selected = items.data?.items.find(
    (item) => itemKey(item) === selectedKey,
  );
  const close = () => update({ item: "", source: "" });
  const search = (event: FormEvent) => {
    event.preventDefault();
    update({ q: input.trim(), item: "" });
  };
  const reasons = summary.data?.reasons ?? [];
  return (
    <div className="library-review">
      <div className="page-heading">
        <div>
          <h1>Library review</h1>
          <p className="muted">
            Books from your connected libraries that Dewarr couldn't match or
            read. Link each one to the right book so it counts as owned.
          </p>
        </div>
      </div>
      <nav className="settings-tabs review-tabs" aria-label="Review filter">
        {KINDS.map(({ id, label, count }) => {
          const total = summary.data ? count(summary.data) : 0;
          const next = new URLSearchParams(params);
          next.delete("item");
          next.delete("source");
          if (id === "all") next.delete("kind");
          else next.set("kind", id);
          return (
            <Link
              key={id}
              to={`?${next}`}
              aria-current={kind === id ? "page" : undefined}
            >
              {label}
              {total > 0 && (
                <span className="requests-tab-count">
                  {total > 999 ? "999+" : total}
                </span>
              )}
            </Link>
          );
        })}
      </nav>
      <div className="review-toolbar">
        <form
          className="library-search review-search"
          role="search"
          onSubmit={search}
        >
          <label>
            <span className="sr-only">Search review items</span>
            <input
              type="search"
              placeholder="Search by title or author"
              value={input}
              maxLength={300}
              onChange={(event) => setInput(event.target.value)}
            />
          </label>
        </form>
        {reasons.length > 0 && (
          <div className="review-insights" aria-label="Most common problems">
            <span>Most common problems</span>
            <ul>
              {reasons.slice(0, 4).map((row) => (
                <li key={row.reason}>
                  {reasonLabel(row.reason)}
                  <strong>{row.count}</strong>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
      {resolved && (
        <p className="review-resolved" role="status">
          <CheckCircle2 size={16} aria-hidden />
          {resolved}
          <button type="button" onClick={() => setResolved("")}>
            Dismiss
          </button>
        </p>
      )}
      <Notice error={summary.error || items.error} />
      {items.isPending ? (
        <Loading />
      ) : items.data?.items.length ? (
        <div className="review-grid">
          {items.data.items.map((item) => (
            <ReviewCard
              key={itemKey(item)}
              item={item}
              open={() => update({ item: itemKey(item) })}
            />
          ))}
        </div>
      ) : (
        !items.isError && (
          <Empty title={q ? "No review items match" : "All caught up"}>
            {q
              ? "Try another title or author."
              : "Every library item is matched and read cleanly."}
          </Empty>
        )
      )}
      <InfiniteScroll query={items} />
      {selected && (
        <BookDialog
          key={itemKey(selected)}
          title="Review library item"
          close={close}
          className="review-dialog"
        >
          {selected.asset ? (
            <AssetReview
              asset={selected.asset}
              close={close}
              matched={(workId) => {
                setResolved(
                  workId
                    ? `Linked “${itemTitle(selected)}”. It now counts as owned.`
                    : `Left “${itemTitle(selected)}” unmatched.`,
                );
                close();
              }}
            />
          ) : (
            <ReadIssueReview issue={selected.read_issue!} />
          )}
        </BookDialog>
      )}
    </div>
  );
}

function Chip({
  tone,
  children,
}: {
  tone: "match" | "warn" | "error";
  children: string;
}) {
  return (
    <span className="review-chip" data-tone={tone}>
      {children}
    </span>
  );
}

function ReviewCard({ item, open }: { item: Item; open: () => void }) {
  const asset = item.asset;
  const issue = item.read_issue;
  const title = itemTitle(item);
  const authors = (asset ? asset.authors : issue!.authors) ?? [];
  const reasons = (asset ? asset.read_issues : issue!.reasons) ?? [];
  const kind = asset ? asset.server_kind : issue!.server_kind;
  const format = asset
    ? asset.medium === "audio"
      ? "Audiobook"
      : "Ebook"
    : "Files not read";
  return (
    <article className="review-card" aria-label={title}>
      <div className="review-card-body">
        <div className="request-cover" aria-hidden="true">
          <span>{title}</span>
        </div>
        <div className="review-card-main">
          <h2>{title}</h2>
          <p className="muted">{authors.join(", ") || "Unknown author"}</p>
          <p className="review-card-source">
            {format} · {asset ? asset.library_name : issue!.library_name}
          </p>
          <div className="review-chips">
            {asset?.match_status === "needs-review" && (
              <Chip tone="match">Needs matching</Chip>
            )}
            {!asset && <Chip tone="error">Couldn't read files</Chip>}
            {reasons.map((reason) => (
              <Chip tone={asset ? "warn" : "error"} key={reason}>
                {reasonLabel(reason)}
              </Chip>
            ))}
          </div>
        </div>
      </div>
      <footer className="review-card-actions">
        <a
          href={asset ? asset.open_url : issue!.open_url}
          target="_blank"
          rel="noreferrer"
        >
          Open in {appName(kind)}
          <ExternalLink size={13} aria-hidden />
        </a>
        <button className="primary" onClick={open}>
          {asset ? "Find the book" : "See details"}
        </button>
      </footer>
    </article>
  );
}

function BackendReport({
  kind,
  title,
  authors,
  narrators = [],
  library,
  paths,
  reasons,
  openUrl,
}: {
  kind: string;
  title: string;
  authors: string[];
  narrators?: string[];
  library: string;
  paths: string[];
  reasons: string[];
  openUrl: string;
}) {
  return (
    <section className="review-report" aria-label="Library details">
      <div className="review-report-heading">
        <div className="request-cover" aria-hidden="true">
          <span>{title}</span>
        </div>
        <div>
          <p className="review-report-eyebrow">What {appName(kind)} reports</p>
          <h2>{title}</h2>
          <p className="muted">{authors.join(", ") || "No readable author"}</p>
        </div>
      </div>
      <dl>
        {narrators.length > 0 && (
          <div>
            <dt>Narrators</dt>
            <dd>{narrators.join(", ")}</dd>
          </div>
        )}
        <div>
          <dt>Library</dt>
          <dd>{library}</dd>
        </div>
        {reasons.length > 0 && (
          <div>
            <dt>Couldn't read</dt>
            <dd>{reasons.map(reasonLabel).join(", ")}</dd>
          </div>
        )}
        {paths.length > 0 && (
          <div className="review-report-wide">
            <dt>{paths.length === 1 ? "Location" : "Files"}</dt>
            <dd>
              <ul>
                {paths.map((path) => (
                  <li key={path}>
                    <code>{path}</code>
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        )}
      </dl>
      <a href={openUrl} target="_blank" rel="noreferrer">
        Open in {appName(kind)}
        <ExternalLink size={13} aria-hidden />
      </a>
    </section>
  );
}

function AssetReview({
  asset,
  close,
  matched,
}: {
  asset: Asset;
  close: () => void;
  matched: (workId: string | null) => void;
}) {
  const [params] = useSearchParams();
  const source = params.get("source") === "hardcover" ? "hardcover" : "dewarr";
  const match = useAssetMatch(asset, matched);
  const authors = asset.authors ?? [];
  const query = `${asset.title} ${authors[0] || ""}`.trim();
  return (
    <>
      <BackendReport
        kind={asset.server_kind}
        title={asset.title}
        authors={authors}
        narrators={asset.narrators}
        library={asset.library_name}
        paths={(asset.files || []).map((file) => file.path)}
        reasons={asset.read_issues || []}
        openUrl={asset.open_url}
      />
      <section className="review-find" aria-label="Find the book">
        <h3>Which book is this?</h3>
        <nav className="settings-subtabs" aria-label="Where to look">
          {SOURCES.map(([id, label]) => {
            const next = new URLSearchParams(params);
            if (id === "dewarr") next.delete("source");
            else next.set("source", id);
            return (
              <Link
                key={id}
                to={`?${next}`}
                replace
                aria-current={source === id ? "page" : undefined}
              >
                {label}
              </Link>
            );
          })}
        </nav>
        {source === "dewarr" ? (
          <AssetMatchForm
            asset={asset}
            close={close}
            onMatched={matched}
            heading={false}
          />
        ) : (
          <div className="book-match-search">
            <Notice error={match.error} />
            {match.isPending && <Loading label="Linking…" />}
            <ProviderSearch
              canEdit
              initialQuery={query}
              onImported={(work) => match.mutate(work.id)}
            />
          </div>
        )}
      </section>
    </>
  );
}

function ReadIssueReview({ issue }: { issue: ReadIssue }) {
  return (
    <>
      <BackendReport
        kind={issue.server_kind}
        title={issue.title}
        authors={issue.authors}
        library={issue.library_name}
        paths={issue.path ? [issue.path] : []}
        reasons={issue.reasons}
        openUrl={issue.open_url}
      />
      <div className="review-next-step">
        <h3>How to fix it</h3>
        <p>
          {`Dewarr couldn't read this item's files, so it can't count it as owned or link it to a book yet. Check the item in ${appName(issue.server_kind)}, then run a library sync. Once its files can be read, it moves to Needs matching.`}
        </p>
      </div>
    </>
  );
}
