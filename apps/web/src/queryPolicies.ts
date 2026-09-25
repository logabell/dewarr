/** Retain browsed pages briefly; server responses still project current access. */
export const browseCache = { gcTime: 10 * 60_000 };

/** Poll only visible, successful reads whose server cache is being refreshed. */
export function refreshPending(query: {
  state: {
    status: string;
    data?: unknown;
  };
}) {
  if (query.state.status === "error") return false;
  const data = query.state.data;
  const stale = (value: unknown) =>
    !!value &&
    typeof value === "object" &&
    "stale" in value &&
    value.stale === true;
  const pages =
    data && typeof data === "object" && "pages" in data
      ? data.pages
      : undefined;
  return stale(data) || (Array.isArray(pages) && pages.some(stale))
    ? 30_000
    : false;
}
