import { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
export default function ShelfPagination({
  page,
  hasMore,
  busy,
  onPage,
  label,
  max = 100,
  infinite,
}: {
  page: number;
  hasMore: boolean;
  busy: boolean;
  onPage: (page: number) => void;
  label: string;
  max?: number;
  infinite?: {
    fetchNextPage: () => Promise<unknown>;
    isFetchNextPageError: boolean;
    count: number;
  };
}) {
  const nav = useRef<HTMLElement>(null);
  const [scroll, setScroll] = useState({ previous: false, next: false });
  function shelf() {
    return nav.current
      ?.closest(".discovery-section")
      ?.querySelector<HTMLElement>(".discovery-shelf");
  }
  useEffect(() => {
    const element = shelf();
    if (!element) return;
    const update = () =>
      setScroll({
        previous: element.scrollLeft > 2,
        next:
          element.scrollLeft + element.clientWidth < element.scrollWidth - 2,
      });
    update();
    element.addEventListener("scroll", update, { passive: true });
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => {
      element.removeEventListener("scroll", update);
      observer.disconnect();
    };
  }, [page, busy]);
  useEffect(() => {
    const element = shelf();
    if (element && !infinite) element.scrollLeft = 0;
  }, [page, !!infinite]);
  useEffect(() => {
    const element = shelf();
    const last = element?.lastElementChild;
    if (
      !infinite ||
      !element ||
      !last ||
      !hasMore ||
      busy ||
      infinite.isFetchNextPageError
    )
      return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          observer.disconnect();
          void infinite.fetchNextPage();
        }
      },
      { root: element, rootMargin: "0px 160px 0px 0px" },
    );
    observer.observe(last);
    return () => observer.disconnect();
  }, [
    infinite?.count,
    infinite?.fetchNextPage,
    infinite?.isFetchNextPageError,
    hasMore,
    busy,
  ]);
  function move(direction: -1 | 1) {
    const element = shelf();
    if (element && (direction === 1 ? scroll.next : scroll.previous)) {
      element.scrollBy({
        left: direction * element.clientWidth * 0.9,
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "instant"
          : "smooth",
      });
    } else if (infinite && direction === 1) void infinite.fetchNextPage();
    else if (!infinite) onPage(page + direction);
  }
  return (
    <nav
      ref={nav}
      className="shelf-pagination"
      aria-label={`${label} navigation`}
    >
      {busy && <span role="status">Loading…</span>}
      {(scroll.previous || scroll.next || hasMore || page > 1) && (
        <>
          <button
            type="button"
            aria-label={`Scroll ${label} back`}
            disabled={(!scroll.previous && page <= 1) || busy}
            onClick={() => move(-1)}
          >
            <ChevronLeft size={18} />
          </button>
          <button
            type="button"
            aria-label={
              infinite?.isFetchNextPageError
                ? `Retry loading ${label}`
                : `Scroll ${label} forward`
            }
            disabled={(!scroll.next && (!hasMore || page >= max)) || busy}
            onClick={() => move(1)}
          >
            <ChevronRight size={18} />
          </button>
        </>
      )}
    </nav>
  );
}
