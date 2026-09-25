import ConnectionHealth from "./components/ConnectionHealth";
import { ApplicationRelease } from "./components/ApplicationRelease";
import { useRefreshReadingLists } from "./hooks/useRefreshReadingLists";
import type { ReadingProvider } from "./hooks/useRefreshReadingLists";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Download,
  BookOpen,
  Compass,
  ListPlus,
  RefreshCw,
  ChevronDown,
  ListChecks,
  LogOut,
  Search,
  Settings,
} from "lucide-react";
import {
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { api, ApiError, result, setCsrf } from "./api/client";
import type { Auth } from "./api/client";
import { Loading, Notice } from "./components";
import { usePendingApprovals } from "./hooks/usePendingApprovals";
import { useLibraryReviewCount } from "./hooks/useLibraryReviewCount";
import { canManageOwnRequests } from "./permissions";

const AddDiscoveryList = lazy(() => import("./pages/AddDiscoveryList"));
const SettingsPage = lazy(() => import("./pages/Settings"));
const GettingStarted = lazy(() => import("./pages/GettingStarted"));
const DiscoverBook = lazy(() => import("./pages/DiscoverBook"));
const Discover = lazy(() => import("./pages/Discover"));
const SeriesGapPage = lazy(() =>
  import("./components/SeriesContinuation").then((module) => ({
    default: module.SeriesGapPage,
  })),
);
const CommunityLists = lazy(() => import("./pages/CommunityLists"));
const BookDetail = lazy(() => import("./pages/BookDetail"));
const Following = lazy(() => import("./pages/Following"));
const AuthorDetail = lazy(() => import("./pages/AuthorDetail"));
const Series = lazy(() => import("./pages/Series"));
const Lists = lazy(() => import("./pages/Lists"));
const RequestsPage = lazy(() => import("./pages/Requests"));
const SourceArtifact = lazy(() => import("./pages/SourceArtifact"));
const MyLibrary = lazy(() => import("./pages/MyLibrary"));
const ProviderSearch = lazy(() => import("./pages/ProviderSearch"));
const ImportReview = lazy(() => import("./pages/ImportReview"));
const LibraryReview = lazy(() => import("./pages/LibraryReview"));
const Recovery = lazy(() => import("./pages/Recovery"));

export default function App() {
  const client = useQueryClient();
  useEffect(() => {
    const expire = () => {
      setCsrf("");
      // A new document discards private query caches and outstanding requests.
      window.location.replace("/");
    };
    window.addEventListener("book:session-expired", expire);
    return () => window.removeEventListener("book:session-expired", expire);
  }, [client]);
  const session = useQuery({
    queryKey: ["session"],
    queryFn: async () => {
      const response = await api.GET("/api/auth/me");
      if (response.response.status === 401) return null;
      const auth = result(response);
      setCsrf(auth.csrf_token);
      return auth;
    },
  });
  if (session.isPending) return <Loading />;
  if (session.isError)
    return (
      <main className="auth-page">
        <div className="panel">
          <h1>Unable to connect</h1>
          <Notice error={session.error} />
          <button onClick={() => session.refetch()}>Try again</button>
        </div>
      </main>
    );
  if (!session.data)
    return (
      <SignIn
        onSuccess={(auth) => {
          setCsrf(auth.csrf_token);
          client.setQueryData(["session"], auth);
        }}
      />
    );
  if (session.data.recovery)
    return (
      <Suspense fallback={<Loading />}>
        <Recovery />
      </Suspense>
    );
  return <Shell auth={session.data} />;
}

const OIDC_ERRORS: Record<string, string> = {
  denied: "Your identity provider did not sign you in.",
  mismatch: "That sign-in attempt expired. Try again.",
  rejected: "This account cannot sign in with the identity provider.",
  unavailable: "The identity provider could not be reached.",
  paused: "Sign-in is paused during recovery review.",
  limited: "Too many sign-in attempts. Try again in ten minutes.",
};

const PLEX_ERRORS: Record<string, string> = {
  denied: "Plex did not sign you in.",
  mismatch: "That sign-in attempt expired. Try again.",
  rejected: "This Plex account cannot sign in.",
  unavailable: "Plex could not be reached.",
  paused: "Sign-in is paused during recovery review.",
  limited: "Too many sign-in attempts. Try again in ten minutes.",
};

function SignIn({ onSuccess }: { onSuccess: (auth: Auth) => void }) {
  const [oidcError] = useState(() => {
    const code = new URLSearchParams(window.location.search).get("oidc_error");
    return code ? OIDC_ERRORS[code] : "";
  });
  const [plexError] = useState(() => {
    const code = new URLSearchParams(window.location.search).get("plex_error");
    return code ? PLEX_ERRORS[code] : "";
  });
  useEffect(() => {
    if (!oidcError && !plexError) return;
    const url = new URL(window.location.href);
    url.searchParams.delete("oidc_error");
    url.searchParams.delete("plex_error");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }, [oidcError, plexError]);
  const setup = useQuery({
    queryKey: ["setup"],
    queryFn: async () => result(await api.GET("/api/auth/setup")),
  });
  const oidc = useQuery({
    queryKey: ["oidc-status"],
    queryFn: async () => result(await api.GET("/api/auth/oidc")),
  });
  const plex = useQuery({
    queryKey: ["plex-status"],
    queryFn: async () => result(await api.GET("/api/auth/plex")),
  });
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const mutation = useMutation({
    mutationFn: async () => {
      if (setup.data?.needs_setup)
        return result(
          await api.POST("/api/auth/bootstrap", {
            body: {
              username,
              password,
              display_name: displayName,
            },
          }),
        );
      return result(
        await api.POST("/api/auth/login", { body: { username, password } }),
      );
    },
    onSuccess,
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) setup.refetch();
    },
  });
  if (setup.isPending) return <Loading />;
  return (
    <main className="auth-page">
      <div className="auth-intro">
        <div className="brand">
          <img src="/assets/dewarr.png" width="32" height="32" alt="" />
          <span>Dewarr</span>
        </div>
        <h1>
          Your next chapter
          <br />
          starts here.
        </h1>
        <p>
          A home for your reading lists.
          <br />A clear view of the books you own.
        </p>
        <div className="spines" aria-hidden="true">
          <i />
          <i />
          <i />
          <i />
          <i />
        </div>
      </div>
      <form
        className="panel auth-form"
        onSubmit={(event) => {
          event.preventDefault();
          mutation.mutate();
        }}
      >
        <p className="eyebrow">YOUR PERSONAL BOOKSHELF</p>
        <h2>
          {setup.data?.needs_setup ? "Set up your library" : "Welcome back"}
        </h2>
        <p className="muted">
          {setup.data?.needs_setup
            ? "Create the administrator account for this installation."
            : "Sign in to browse your catalog and lists."}
        </p>
        <Notice
          error={
            setup.error ||
            mutation.error ||
            (oidcError ? new Error(oidcError) : null) ||
            (plexError ? new Error(plexError) : null)
          }
        />
        {setup.data?.needs_setup ? (
          <label>
            Your name
            <input
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              autoComplete="name"
              required
              maxLength={120}
            />
          </label>
        ) : null}
        <label>
          Username
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            required
            minLength={3}
            maxLength={100}
            pattern="[A-Za-z0-9_.@\-]+"
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={
              setup.data?.needs_setup ? "new-password" : "current-password"
            }
            required
            minLength={12}
            maxLength={256}
          />
        </label>
        <button
          className="primary"
          disabled={mutation.isPending || setup.isError}
        >
          {mutation.isPending
            ? "Connecting…"
            : setup.data?.needs_setup
              ? "Create administrator"
              : "Sign in"}
        </button>
        {!setup.data?.needs_setup &&
        ((oidc.data?.enabled && oidc.data.label) || plex.data?.enabled) ? (
          <>
            <p className="auth-divider">or</p>
            {oidc.data?.enabled && oidc.data.label ? (
              <a className="auth-provider" href="/api/auth/oidc/start">
                Sign in with {oidc.data.label}
              </a>
            ) : null}
            {plex.data?.enabled ? (
              <a className="auth-provider" href="/api/auth/plex/start">
                Sign in with Plex
              </a>
            ) : null}
          </>
        ) : null}
      </form>
    </main>
  );
}

function RefreshLists({
  refresh,
}: {
  refresh: ReturnType<typeof useRefreshReadingLists>;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  function choose(provider?: ReadingProvider) {
    setOpen(false);
    refresh.refresh(provider);
  }
  return (
    <div
      className="list-refresh"
      data-busy={refresh.busy ? "true" : "false"}
      ref={root}
    >
      <button
        className="topbar-action list-refresh-main"
        aria-label="Refresh lists"
        title="Refresh tracked Goodreads, StoryGraph, and Hardcover lists. Community lists keep their own schedule."
        disabled={refresh.busy}
        onClick={() => choose()}
      >
        <RefreshCw
          size={18}
          aria-hidden="true"
          className={refresh.busy ? "list-refresh-spinning" : undefined}
        />
        <span className="list-refresh-label">
          {refresh.busy ? "Refreshing…" : "Refresh lists"}
        </span>
      </button>
      <button
        type="button"
        className="list-refresh-toggle"
        aria-label="Choose which lists to refresh"
        aria-expanded={open}
        aria-haspopup="menu"
        disabled={refresh.busy}
        onClick={() => setOpen((value) => !value)}
      >
        <ChevronDown size={16} aria-hidden="true" />
      </button>
      {open && (
        <div className="list-refresh-menu" role="menu">
          {(
            [
              ["goodreads", "Goodreads"],
              ["storygraph", "StoryGraph"],
              ["hardcover", "Hardcover"],
            ] as const
          ).map(([provider, name]) => (
            <button
              key={provider}
              type="button"
              role="menuitem"
              disabled={refresh.busy}
              onClick={() => choose(provider)}
            >
              {name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Shell({ auth }: { auth: Auth }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const location = useLocation();
  const [search, setSearch] = useState("");
  const [addingList, setAddingList] = useState(false);
  const listsRefresh = useRefreshReadingLists();
  useEffect(() => {
    if (location.pathname === "/search")
      setSearch(new URLSearchParams(location.search).get("q") || "");
  }, [location.pathname, location.search]);
  const permissions = auth.user.permissions ?? [];
  const canApprove =
    auth.user.role === "admin" || permissions.includes("manage_requests");
  const pendingApprovals = usePendingApprovals(canApprove);
  const waiting = pendingApprovals.data?.total ?? 0;
  const admin = auth.user.role === "admin";
  const canEdit = auth.user.role !== "viewer";
  const reviewing = useLibraryReviewCount(admin).data?.total ?? 0;
  const logout = useMutation({
    mutationFn: async () => result(await api.POST("/api/auth/logout")),
    onSuccess: () => {
      setCsrf("");
      client.clear();
      window.location.assign("/");
    },
  });
  function searchSubmit(event: FormEvent) {
    event.preventDefault();
    navigate("/search?q=" + encodeURIComponent(search.trim()));
  }
  if (
    auth.user.onboarding_status === "pending" &&
    location.pathname !== "/onboarding"
  )
    return <Navigate to="/onboarding" replace />;
  if (location.pathname === "/onboarding")
    return (
      <main className="onboarding-shell">
        <Suspense fallback={<Loading />}>
          <GettingStarted role={auth.user.role} />
        </Suspense>
      </main>
    );
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <aside className="sidebar">
        <NavLink to="/" className="brand">
          <img src="/assets/dewarr.png" width="32" height="32" alt="" />
          <span>Dewarr</span>
        </NavLink>
        <button
          className="icon-button mobile-signout"
          aria-label="Sign out"
          onClick={() => logout.mutate()}
          disabled={logout.isPending}
        >
          <LogOut size={18} />
        </button>
        <p className="nav-caption">YOUR COLLECTION</p>
        <nav aria-label="Main navigation">
          <NavLink to="/discover">
            <Compass size={19} />
            Discover
          </NavLink>
          {canEdit && (
            <NavLink to="/following">
              <ListPlus size={19} />
              Following
            </NavLink>
          )}
          <NavLink to="/library" end>
            <BookOpen size={19} />
            My Library
          </NavLink>
          {admin && (
            <NavLink to="/review">
              <ListChecks size={19} />
              Review
              {reviewing > 0 && (
                <span className="nav-count">
                  {reviewing > 99 ? "99+" : reviewing}
                  <span className="sr-only"> library items to review</span>
                </span>
              )}
            </NavLink>
          )}
          <NavLink to={waiting > 0 ? "/requests?status=pending" : "/requests"}>
            <Download size={19} />
            Requests
            {waiting > 0 && (
              <span className="nav-count">
                {waiting > 99 ? "99+" : waiting}
                <span className="sr-only"> waiting for approval</span>
              </span>
            )}
          </NavLink>
          <NavLink to="/settings">
            <Settings size={19} />
            Settings
          </NavLink>
        </nav>
        <div className="sidebar-bottom">
          <div className="avatar">
            {auth.user.display_name.slice(0, 1).toUpperCase()}
          </div>
          <div>
            <strong>{auth.user.display_name}</strong>
            <small>{auth.user.access_label}</small>
          </div>
          <button
            className="icon-button"
            aria-label="Sign out"
            onClick={() => logout.mutate()}
            disabled={logout.isPending}
          >
            <LogOut size={18} />
          </button>
        </div>
        <ApplicationRelease />
      </aside>
      <div className="workspace">
        <header className="topbar">
          <form className="search" role="search" onSubmit={searchSubmit}>
            <Search size={18} aria-hidden="true" />
            <input
              aria-label="Search books or authors"
              placeholder="Search books or authors"
              maxLength={300}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <button type="submit">Search</button>
          </form>
          <ConnectionHealth />
          {auth.user.role !== "viewer" && (
            <div className="topbar-actions">
              <button
                className="topbar-action"
                onClick={() => setAddingList(true)}
              >
                <ListPlus size={18} aria-hidden="true" />
                Add list
              </button>
              <RefreshLists refresh={listsRefresh} />
            </div>
          )}
        </header>
        {addingList && (
          <Suspense fallback={<Loading />}>
            <AddDiscoveryList close={() => setAddingList(false)} />
          </Suspense>
        )}
        <main id="main" className="main-content">
          <Notice error={logout.error} />
          <Notice error={listsRefresh.error} />
          {(listsRefresh.busy || listsRefresh.message) && (
            <p role="status">
              {listsRefresh.busy ? "Refreshing lists…" : listsRefresh.message}
            </p>
          )}
          <Suspense fallback={<Loading />}>
            <Routes>
              <Route
                path="/getting-started"
                element={<SettingsRedirect to="/onboarding" />}
              />
              <Route
                path="/settings"
                element={
                  <SettingsPage
                    role={auth.user.role}
                    permissions={permissions}
                  />
                }
              />
              <Route
                path="/following"
                element={canEdit ? <Following /> : <Navigate to="/" replace />}
              />
              <Route
                path="/authors/hardcover/:externalId"
                element={<AuthorDetail canEdit={canEdit} />}
              />
              <Route
                path="/discover"
                element={<Discover canEdit={auth.user.role !== "viewer"} />}
              />
              <Route
                path="/discover/series"
                element={
                  <SeriesGapPage canEdit={auth.user.role !== "viewer"} />
                }
              />
              <Route
                path="/discover/collections/:collectionId"
                element={<Discover canEdit={auth.user.role !== "viewer"} />}
              />
              <Route
                path="/discover/books/:provider/:externalId"
                element={<DiscoverBook canEdit={auth.user.role !== "viewer"} />}
              />
              <Route
                path="/discover/lists"
                element={
                  <CommunityLists canEdit={auth.user.role !== "viewer"} />
                }
              />
              <Route
                path="/discover/lists/:externalId"
                element={
                  <CommunityLists canEdit={auth.user.role !== "viewer"} />
                }
              />
              <Route
                path="/download-preferences"
                element={<SettingsRedirect to="/settings#preferences" />}
              />
              <Route path="/" element={<SettingsRedirect to="/library" />} />
              <Route
                path="/books/:id"
                element={
                  <BookDetail
                    canEdit={auth.user.role !== "viewer"}
                    admin={auth.user.role === "admin"}
                  />
                }
              />
              <Route
                path="/search"
                element={
                  <ProviderSearch canEdit={auth.user.role !== "viewer"} />
                }
              />
              <Route
                path="/series/hardcover/:externalId"
                element={<Series canEdit={auth.user.role !== "viewer"} />}
              />
              <Route
                path="/metadata"
                element={<SettingsRedirect to="/settings#catalog" />}
              />
              <Route
                path="/sources"
                element={<SettingsRedirect to="/search" />}
              />
              <Route
                path="/sources/audiobookbay"
                element={<SettingsRedirect to="/search" />}
              />
              <Route
                path="/sources/prowlarr"
                element={<SettingsRedirect to="/search" />}
              />
              <Route
                path="/sources/artifacts/:id"
                element={
                  auth.user.role !== "viewer" ? (
                    <SourceArtifact />
                  ) : (
                    <Navigate to="/" replace />
                  )
                }
              />
              <Route
                path="/downloaders"
                element={<SettingsRedirect to="/settings#downloaders" />}
              />
              <Route
                path="/organization/destinations"
                element={<SettingsRedirect to="/settings#libraries" />}
              />
              <Route
                path="/organization/inspections"
                element={
                  auth.user.role === "admin" ? (
                    <ImportReview />
                  ) : (
                    <Navigate to="/" replace />
                  )
                }
              />
              <Route
                path="/organization"
                element={<SettingsRedirect to="/settings#naming" />}
              />
              <Route
                path="/lists"
                element={<Lists canEdit={auth.user.role !== "viewer"} />}
              />
              <Route
                path="/lists/:id"
                element={<Lists canEdit={auth.user.role !== "viewer"} />}
              />
              <Route
                path="/requests"
                element={
                  <RequestsPage
                    admin={auth.user.role === "admin"}
                    canRequest={canManageOwnRequests(
                      permissions,
                      auth.user.role,
                    )}
                    canApprove={
                      auth.user.role === "admin" ||
                      permissions.includes("manage_requests")
                    }
                  />
                }
              />
              <Route path="/activity" element={<LegacyActivityRedirect />} />
              <Route
                path="/accounts"
                element={<SettingsRedirect to="/settings#accounts" />}
              />
              <Route
                path="/library"
                element={
                  <MyLibrary
                    admin={auth.user.role === "admin"}
                    canEdit={auth.user.role !== "viewer"}
                  />
                }
              />
              <Route
                path="/review"
                element={
                  auth.user.role === "admin" ? (
                    <LibraryReview />
                  ) : (
                    <Navigate to="/library" replace />
                  )
                }
              />
              <Route
                path="/connections"
                element={<SettingsRedirect to="/settings#libraries" />}
              />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </div>
  );
}

function SettingsRedirect({ to }: { to: string }) {
  const location = useLocation();
  const [path, hash] = to.split("#");
  return (
    <Navigate
      to={`${path}${location.search}${hash ? `#${hash}` : ""}`}
      replace
    />
  );
}

function LegacyActivityRedirect() {
  const location = useLocation();
  return (
    <SettingsRedirect
      to={
        location.hash === "#downloads"
          ? "/requests#downloads"
          : "/settings#logs"
      }
    />
  );
}
