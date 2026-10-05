import { useLayoutEffect, useRef } from "react";
import {
  Link,
  useLocation,
  useNavigationType,
  type LinkProps,
} from "react-router-dom";

export function safeReturn(value: unknown, fallback: string) {
  return typeof value === "string" &&
    /^\/(?!\/)/.test(value) &&
    !/[\\\x00-\x1f]/.test(value)
    ? value
    : fallback;
}

export function ContextLink(props: LinkProps) {
  const location = useLocation();
  return (
    <Link
      {...props}
      state={{
        ...props.state,
        returnTo: location.pathname + location.search + location.hash,
        returnParent: location.state?.returnTo,
      }}
    />
  );
}

export function ReturnLink({
  fallback,
  label,
}: {
  fallback: string;
  label: string;
}) {
  const location = useLocation();
  const to = safeReturn(location.state?.returnTo, fallback);
  const name = to.startsWith("/requests")
    ? "requests"
    : to.startsWith("/organization/inspections")
      ? "download review"
      : to.startsWith("/review")
        ? "library review"
        : to.startsWith("/search")
          ? "search results"
          : to.startsWith("/discover")
            ? "Discover"
            : to.startsWith("/library")
              ? "My Library"
              : to.startsWith("/books/")
                ? "book"
                : to.startsWith("/following")
                  ? "Following"
                  : to.startsWith("/lists")
                    ? "reading list"
                    : label;
  return (
    <Link
      to={to}
      state={{ restoreScroll: true, returnTo: location.state?.returnParent }}
      className="back-link"
    >
      ← Back to {name}
    </Link>
  );
}

/** Preserve nearby browsing positions without retaining private data across sessions. */
export default function NavigationContinuity() {
  const location = useLocation();
  const navigation = useNavigationType();
  const positions = useRef(new Map<string, number>());
  const key = location.pathname + location.search;
  const previousPath = useRef(location.pathname);
  useLayoutEffect(() => {
    const samePage = previousPath.current === location.pathname;
    previousPath.current = location.pathname;
    const y =
      navigation === "POP" || location.state?.restoreScroll
        ? positions.current.get(key) || 0
        : samePage
          ? window.scrollY
          : 0;
    let frame = 0;
    const restore = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => window.scrollTo(0, y));
    };
    if (!location.hash) restore();
    const observer = new ResizeObserver(restore);
    if (y && !location.hash) observer.observe(document.body);
    const stop = () => observer.disconnect();
    window.addEventListener("wheel", stop, { passive: true, once: true });
    window.addEventListener("touchstart", stop, { passive: true, once: true });
    const timer = window.setTimeout(stop, 2000);
    return () => {
      positions.current.set(key, window.scrollY);
      if (positions.current.size > 50)
        positions.current.delete(positions.current.keys().next().value!);
      cancelAnimationFrame(frame);
      clearTimeout(timer);
      observer.disconnect();
      window.removeEventListener("wheel", stop);
      window.removeEventListener("touchstart", stop);
    };
  }, [key, navigation]);
  useLayoutEffect(() => {
    const align = () =>
      document
        .querySelectorAll<HTMLElement>(".book-tabs, .page-tabs")
        .forEach((nav) => {
          const active = nav.querySelector<HTMLElement>(
            '[aria-selected="true"], [aria-current="page"]',
          );
          if (!active) return;
          const n = nav.getBoundingClientRect(),
            a = active.getBoundingClientRect();
          if (a.left < n.left || a.right > n.right)
            nav.scrollLeft += a.left - n.left - 12;
        });
    const observer = new ResizeObserver(align);
    const main = document.getElementById("main");
    if (main) observer.observe(main);
    const mutations = new MutationObserver(align);
    if (main) mutations.observe(main, { childList: true, subtree: true });
    align();
    window.addEventListener("resize", align);
    return () => {
      observer.disconnect();
      mutations.disconnect();
      window.removeEventListener("resize", align);
    };
  }, [key]);
  return null;
}
