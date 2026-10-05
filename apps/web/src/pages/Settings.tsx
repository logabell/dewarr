import { Suspense, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { Link, Navigate, useLocation } from "react-router-dom";
import { Loading } from "../components";
import { settingsSections } from "./SettingsSections";

export default function Settings({
  role,
  permissions = [],
}: {
  role: string;
  permissions?: string[];
}) {
  const sections = settingsSections(role, permissions);
  const location = useLocation();
  const tabs = useRef<HTMLElement>(null);
  const [overflow, setOverflow] = useState({ left: false, right: false });
  const selected =
    sections.find((section) => section.id === location.hash.slice(1)) ||
    sections[0];
  useEffect(() => {
    const bar = tabs.current;
    const current = bar?.querySelector("a[aria-current='page']");
    if (!(current instanceof HTMLElement) || !bar) return;
    const measure = () =>
      setOverflow({
        left: bar.scrollLeft > 1,
        right: bar.scrollLeft + bar.clientWidth < bar.scrollWidth - 1,
      });
    const centerTab = () => {
      const left =
        current.offsetLeft - (bar.clientWidth - current.offsetWidth) / 2;
      bar.scrollTo({ left: Math.max(0, left) });
      measure();
    };
    centerTab();
    const observer = new ResizeObserver(centerTab);
    observer.observe(bar);
    bar.addEventListener("scroll", measure, { passive: true });
    return () => {
      observer.disconnect();
      bar.removeEventListener("scroll", measure);
    };
  }, [selected.id, sections.length]);
  if (location.hash === "#recovery" || location.hash === "#quotas") {
    const group = location.hash.slice(1);
    const search = new URLSearchParams(location.search);
    search.set("group", group);
    return (
      <Navigate
        replace
        to={`/settings?${search}#${group === "recovery" ? "downloaders" : "accounts"}`}
      />
    );
  }
  if (location.hash === "#lists")
    return <Navigate replace to={`/settings${location.search}#reading`} />;
  if (location.hash === "#profiles")
    return (
      <Navigate
        replace
        to={`/settings${location.search}#${role === "viewer" ? "display" : "preferences"}`}
      />
    );
  if (location.hash === "#storage")
    return <Navigate replace to={`/settings${location.search}#libraries`} />;
  return (
    <div className="settings-page settings-focused">
      <h1 className="sr-only">Settings</h1>
      <div className="settings-tabs-bar">
        {(overflow.left || overflow.right) && (
          <button
            type="button"
            className="settings-tabs-scroll"
            aria-label="Earlier settings categories"
            disabled={!overflow.left}
            onClick={() =>
              tabs.current?.scrollBy({ left: -tabs.current.clientWidth * 0.75 })
            }
          >
            <ChevronLeft size={18} aria-hidden="true" />
          </button>
        )}
        <nav ref={tabs} className="page-tabs" aria-label="Settings categories">
          {sections.map((item) => (
            <Link
              key={item.id}
              to={`/settings#${item.id}`}
              aria-current={selected.id === item.id ? "page" : undefined}
            >
              {item.title}
            </Link>
          ))}
        </nav>
        {(overflow.left || overflow.right) && (
          <button
            type="button"
            className="settings-tabs-scroll"
            aria-label="More settings categories"
            disabled={!overflow.right}
            onClick={() =>
              tabs.current?.scrollBy({ left: tabs.current.clientWidth * 0.75 })
            }
          >
            <ChevronRight size={18} aria-hidden="true" />
          </button>
        )}
      </div>
      <section
        id={selected.id}
        className="settings-section"
        aria-labelledby="settings-section-title"
      >
        <header className="settings-section-heading">
          <h2 id="settings-section-title">{selected.title}</h2>
          {role === "admin" &&
            [
              "libraries",
              "sources",
              "downloaders",
              "storage",
              "naming",
              "accounts",
            ].includes(selected.id) && (
              <span className="settings-scope">Administrator</span>
            )}
        </header>
        <div className="settings-section-body">
          <Suspense key={selected.id} fallback={<Loading />}>
            {selected.content}
          </Suspense>
        </div>
      </section>
    </div>
  );
}
