export default function ContentSkeleton({
  kind = "rows",
}: {
  kind?: "book" | "rows";
}) {
  return (
    <div
      className={`content-skeleton skeleton-${kind}`}
      role="status"
      aria-label="Loading content"
    >
      <span className="sr-only">Loading…</span>
      <div aria-hidden="true" className="skeleton-cover" />
      <div aria-hidden="true" className="skeleton-lines">
        {[0, 1, 2].map((i) => (
          <div key={i} />
        ))}
      </div>
    </div>
  );
}
