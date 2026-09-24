import SettingHelp from "../components/SettingHelp";
import { lazy, Suspense, useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Check, ArrowRight } from "lucide-react";
import { api, result, type Auth } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import { settingsSections } from "./SettingsSections";

function setupCount(
  count: number,
  singular: string,
  plural: string,
  empty = "None connected",
) {
  if (count === 0) return empty;
  return `${count} ${count === 1 ? singular : plural}`;
}

const Libraries = lazy(() => import("./Connections"));
const Destinations = lazy(() => import("./Destinations"));
const ReadingAccounts = lazy(() => import("./ReadingAccounts"));
type Progress = components["schemas"]["OnboardingProgress"];
export default function GettingStarted({ role }: { role: string }) {
  const admin = role === "admin";
  const cache = useQueryClient();
  const navigate = useNavigate();
  const heading = useRef<HTMLHeadingElement>(null);
  const progress = useQuery({
    queryKey: ["onboarding"],
    queryFn: async () => result(await api.GET("/api/setup/onboarding")),
  });
  const readiness = useQuery({
    queryKey: ["setup-readiness"],
    enabled: admin,
    queryFn: async () => result(await api.GET("/api/setup/readiness")),
    refetchInterval: 15000,
  });
  // Preserve saved step numbers independently of the settings tab layout.
  const all = settingsSections(role).map((section) =>
    admin && section.id === "libraries"
      ? { ...section, content: <Libraries embedded connectionOnly /> }
      : section,
  );
  if (admin)
    all.push({
      id: "storage",
      title: "Library folders",
      content: <Destinations embedded />,
    });
  const ids = admin
    ? [
        "catalog",
        "libraries",
        "sources",
        "downloaders",
        "storage",
        "reading",
        "finish",
      ]
    : role === "viewer"
      ? ["catalog", "finish"]
      : ["catalog", "preferences", "reading", "finish"];
  const descriptions: Record<string, string> = {
    catalog:
      "Connect Hardcover for discovery, or use Open Library without an account.",
    reading:
      "Connect Goodreads, StoryGraph, or Hardcover once, then choose the lists you want to keep up with.",
    libraries:
      "Connect Audiobookshelf or Grimmory to see the books you already own.",
    sources: "Choose where to find ebook and audiobook releases.",
    downloaders:
      "Connect qBittorrent for torrents, or SABnzbd or NZBGet for Usenet.",
    storage: "Choose where imported books will be saved.",
    preferences: "Choose the formats and sources you prefer.",
    finish:
      "Your saved settings are ready. You can change them anytime in Settings.",
  };
  const current = Math.min(
    ["completed", "skipped"].includes(progress.data?.status || "")
      ? 0
      : progress.data?.step || 0,
    ids.length - 1,
  );
  const id = ids[current];
  const section = all.find((section) => section.id === id);
  const update = useMutation({
    mutationFn: async (next: Progress) =>
      result(await api.PUT("/api/setup/onboarding", { body: next })),
    onSuccess: (next) => {
      cache.setQueryData(["onboarding"], next);
      cache.setQueryData<Auth>(["session"], (old) =>
        old
          ? {
              ...old,
              user: {
                ...old.user,
                onboarding_status: next.status || "pending",
              },
            }
          : old,
      );
      cache.invalidateQueries({ queryKey: ["setup-readiness"] });
      if (next.status !== "pending") navigate("/discover", { replace: true });
    },
  });
  function move(step: number, skip = false) {
    update.mutate({
      status: "pending",
      step,
      skipped: skip
        ? [...new Set([...(progress.data?.skipped || []), current])]
        : progress.data?.skipped || [],
    });
  }
  function leave(status: "completed" | "deferred") {
    update.mutate({
      status,
      step: current,
      skipped: progress.data?.skipped || [],
    });
  }
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }, [current, progress.isSuccess]);
  if (progress.isPending) return <Loading />;
  return (
    <div className="onboarding-page">
      <header className="onboarding-header">
        <span className="brand">
          <img src="/assets/dewarr.png" width="32" height="32" alt="" />
          <span>Dewarr</span>
        </span>
        <button disabled={update.isPending} onClick={() => leave("deferred")}>
          Finish later
        </button>
      </header>
      <div className="onboarding-intro">
        <h1>Let’s set up your library</h1>
        <p>Every step is optional.</p>
      </div>
      <Notice error={progress.error || update.error} />
      {progress.error ? (
        <button onClick={() => progress.refetch()}>Retry setup</button>
      ) : (
        <div className="onboarding-layout">
          <nav aria-label="Setup steps" className="onboarding-steps">
            {ids.map((key, index) => (
              <button
                key={key}
                disabled={update.isPending}
                aria-current={index === current ? "step" : undefined}
                onClick={() => move(index)}
              >
                <span className="onboarding-step-number">
                  {index < current &&
                  !progress.data?.skipped?.includes(index) ? (
                    <Check size={16} />
                  ) : (
                    index + 1
                  )}
                </span>
                <span>
                  {all.find((section) => section.id === key)?.title ||
                    "Ready to go"}
                  {progress.data?.skipped?.includes(index) && (
                    <small>Skipped</small>
                  )}
                </span>
              </button>
            ))}
          </nav>
          <div className="onboarding-card">
            <p className="eyebrow">
              STEP {current + 1} OF {ids.length}
            </p>
            <div className="onboarding-title">
              <h2 ref={heading} tabIndex={-1}>
                {section?.title || "Ready to go"}
              </h2>
              <SettingHelp label="this setup step">
                {descriptions[id]}
              </SettingHelp>
            </div>
            {id === "finish" ? (
              <div className="onboarding-finish">
                <div className="onboarding-finish-lead">
                  <span className="onboarding-finish-mark" aria-hidden="true">
                    <Check size={18} />
                  </span>
                  <div>
                    <h3>You’re ready to explore</h3>
                    <p>Your setup is saved. Change it anytime in Settings.</p>
                  </div>
                </div>
                {admin && (
                  <>
                    <Notice error={readiness.error} />
                    {readiness.isPending && (
                      <p className="onboarding-finish-note">
                        Checking your setup…
                      </p>
                    )}
                    {readiness.data && (
                      <dl className="onboarding-summary">
                        <div>
                          <dt>Catalog</dt>
                          <dd>
                            {readiness.data.catalog?.enabled
                              ? "Hardcover"
                              : "Open Library"}
                          </dd>
                        </div>
                        <div>
                          <dt>Libraries</dt>
                          <dd>
                            {setupCount(
                              readiness.data.libraries.length,
                              "connection",
                              "connections",
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt>Download sources</dt>
                          <dd>
                            {setupCount(
                              readiness.data.sources.filter(
                                (source) => source.enabled,
                              ).length,
                              "enabled",
                              "enabled",
                              "None enabled",
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt>Download clients</dt>
                          <dd>
                            {setupCount(
                              readiness.data.downloaders.length,
                              "connected",
                              "connected",
                            )}
                          </dd>
                        </div>
                      </dl>
                    )}
                    {readiness.data &&
                      !readiness.data.download_dispatch_enabled && (
                        <p className="onboarding-finish-note">
                          Downloads are disabled on this server. Browsing and
                          lists are ready. To enable downloads, set{" "}
                          <code>BOOK_DOWNLOAD_DISPATCH_ENABLED=true</code> and
                          restart Dewarr.
                        </p>
                      )}
                  </>
                )}
              </div>
            ) : (
              <div className="settings-section-body">
                <Suspense fallback={<Loading />}>
                  {id === "reading" ? (
                    <ReadingAccounts onConfigureHardcover={() => move(0)} />
                  ) : (
                    section?.content
                  )}
                </Suspense>
              </div>
            )}
            {id !== "finish" && (
              <p className="onboarding-save-note">
                Save your changes above before moving to the next step.
              </p>
            )}
            <footer className="onboarding-actions">
              <button
                disabled={current === 0 || update.isPending}
                onClick={() => move(current - 1)}
              >
                Back
              </button>
              <div>
                {id !== "finish" && (
                  <button
                    disabled={update.isPending}
                    onClick={() => move(current + 1, true)}
                  >
                    Skip this step
                  </button>
                )}
                <button
                  className="primary"
                  disabled={update.isPending}
                  onClick={() =>
                    id === "finish" ? leave("completed") : move(current + 1)
                  }
                >
                  {update.isPending
                    ? "Saving…"
                    : id === "finish"
                      ? "Start browsing"
                      : "Next step"}
                  <ArrowRight size={16} />
                </button>
              </div>
            </footer>
          </div>
        </div>
      )}
    </div>
  );
}
