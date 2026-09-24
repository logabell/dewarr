import { Suspense, useEffect } from "react";
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
  const selected =
    sections.find((section) => section.id === location.hash.slice(1)) ||
    sections[0];
  useEffect(() => {
    const current = document.querySelector(".page-tabs a[aria-current='page']");
    const bar = current?.parentElement;
    if (!(current instanceof HTMLElement) || !bar) return;
    const centerTab = () => {
      const left =
        current.offsetLeft - (bar.clientWidth - current.offsetWidth) / 2;
      bar.scrollTo({ left: Math.max(0, left) });
    };
    centerTab();
    const observer = new ResizeObserver(centerTab);
    observer.observe(bar);
    return () => observer.disconnect();
  }, [selected.id]);
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
      <nav className="page-tabs" aria-label="Settings categories">
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
