import { Link, useLocation, useSearchParams } from "react-router-dom";

export default function DetailTabs({
  tabs,
  selected,
  label,
}: {
  tabs: readonly (readonly [string, string])[];
  selected: string;
  label: string;
}) {
  const [params] = useSearchParams();
  const location = useLocation();
  return (
    <nav
      className="reader-nav book-tabs"
      role="tablist"
      aria-label={label}
      onKeyDown={(event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
          return;
        const links = [
          ...event.currentTarget.querySelectorAll<HTMLAnchorElement>(
            '[role="tab"]',
          ),
        ];
        const current = links.indexOf(
          document.activeElement as HTMLAnchorElement,
        );
        const next =
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? links.length - 1
              : (current +
                  (event.key === "ArrowRight" ? 1 : -1) +
                  links.length) %
                links.length;
        event.preventDefault();
        links[next].focus();
        links[next].click();
      }}
    >
      {tabs.map(([key, title]) => {
        const search = new URLSearchParams(params);
        search.set("tab", key);
        return (
          <Link
            key={key}
            id={`detail-tab-${key}`}
            role="tab"
            aria-selected={selected === key}
            tabIndex={selected === key ? 0 : -1}
            aria-controls="detail-tab-panel"
            to={`?${search}`}
            state={location.state}
          >
            {title}
          </Link>
        );
      })}
    </nav>
  );
}
