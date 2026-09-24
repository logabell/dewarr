import { usePagedQuery } from "../hooks/usePagedQuery";
import InfiniteScroll from "../components/InfiniteScroll";
import {
  Check,
  CircleCheck,
  Clock3,
  Pause,
  RefreshCw,
  AlertCircle,
  Ellipsis,
} from "lucide-react";
import SettingHelp from "../components/SettingHelp";
import ConnectionStatus from "../components/ConnectionStatus";
import { lazy, Suspense, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Link,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import { randomUUID } from "../randomUUID";
import { useTrackStoryGraphToRead } from "../hooks/useTrackStoryGraphToRead";

const ReadingListDetails = lazy(() => import("./ReadingListDetails"));

type Subscription = components["schemas"]["ReadingSubscription"];
type Choice = { external_id: string; name: string; count: number | null };
type Provider = "goodreads" | "hardcover" | "storygraph";

function ConnectionEditor({
  connected,
  openLabel,
  children,
}: {
  connected: boolean;
  openLabel: string;
  children: ReactNode;
}) {
  if (!connected) return children;
  return (
    <details>
      <summary>{openLabel}</summary>
      {children}
    </details>
  );
}

function ReadingSection({
  label,
  title,
  status,
  help,
  className = "",
  children,
}: {
  label: string;
  title: string;
  status: ReactNode;
  help?: ReactNode;
  className?: string;
  children: ReactNode;
}) {
  const location = useLocation();
  const [open, setOpen] = useState(location.pathname === "/onboarding");
  return (
    <section
      className={`reading-account ${className}`.trim()}
      aria-label={label}
    >
      <details
        className="reading-connection"
        open={open}
        onToggle={(event) => setOpen(event.currentTarget.open)}
      >
        <summary>
          <span className="reading-account-heading">
            <span>{title}</span>
            {help}
          </span>
          {typeof status === "string" ? (
            <ConnectionStatus status={status} />
          ) : (
            status
          )}
        </summary>
        <div className="reading-connection-body">{children}</div>
      </details>
    </section>
  );
}

export default function ReadingAccounts({
  onConfigureHardcover,
}: { onConfigureHardcover?: () => void } = {}) {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const inSettings = useLocation().pathname === "/settings";
  const selectedId = inSettings ? params.get("list") : null;
  const subscriptions = useQuery({
    queryKey: ["reading-subscriptions"],
    queryFn: async () =>
      result(await api.GET("/api/reading-accounts/subscriptions")),
    refetchInterval: (query) =>
      query.state.status === "error" ? false : 30000,
    retry: false,
  });
  return (
    <div className="reading-accounts">
      <div className="reading-overview">
        <p className="muted">
          Choose the shelves to keep in sync with your reading accounts.
        </p>
        {inSettings && (
          <Link to="/discover?view=yours">Browse your lists →</Link>
        )}
      </div>
      <Notice error={subscriptions.error} />
      {subscriptions.isPending && <Loading />}
      <GoodreadsConnection
        subscriptions={subscriptions.data || []}
        ready={subscriptions.isSuccess}
      />
      <StoryGraphConnection
        subscriptions={subscriptions.data || []}
        ready={subscriptions.isSuccess}
      />
      <HardcoverConnection
        onConfigure={onConfigureHardcover}
        subscriptions={subscriptions.data || []}
        ready={subscriptions.isSuccess}
      />
      {inSettings && subscriptions.isSuccess && (
        <LocalLists subscriptions={subscriptions.data} />
      )}
      {selectedId && (
        <Suspense fallback={<Loading />}>
          <ReadingListDetails
            listId={selectedId}
            tracked={
              !!subscriptions.data?.some((s) => s.list_id === selectedId)
            }
            close={() => navigate("/settings#reading", { replace: true })}
          />
        </Suspense>
      )}
    </div>
  );
}

function GoodreadsConnection({
  subscriptions,
  ready,
}: {
  subscriptions: Subscription[];
  ready: boolean;
}) {
  const cache = useQueryClient();
  const [profile, setProfile] = useState("");
  const account = useQuery({
    queryKey: ["goodreads-account"],
    queryFn: async () =>
      result(await api.GET("/api/reading-accounts/goodreads")),
  });
  const connect = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/reading-accounts/goodreads", { body: { profile } }),
      ),
    onSuccess: (value) => {
      cache.setQueryData(["goodreads-account"], value);
      setProfile("");
    },
  });
  const discover = useMutation({
    mutationFn: async () =>
      result(await api.POST("/api/reading-accounts/goodreads/discover")),
    onSuccess: (value) => cache.setQueryData(["goodreads-account"], value),
  });
  const data = account.data;
  return (
    <ReadingSection
      label="Goodreads connection"
      title="Goodreads"
      help={
        <SettingHelp label="Goodreads">
          Paste your profile, My Books address, user ID or shelf RSS link.
          Goodreads feeds may contain only part of a shelf.
        </SettingHelp>
      }
      status={
        account.isPending
          ? "checking"
          : account.isError
            ? "unavailable"
            : data
              ? "connected"
              : "not-configured"
      }
    >
      <div className="reading-connection-tools">
        {data && (
          <a href={data.profile_url} target="_blank" rel="noopener noreferrer">
            {data.name} ↗
          </a>
        )}
        <a
          className="reading-sign-in"
          href="https://www.goodreads.com/review/list"
          target="_blank"
          rel="noopener noreferrer"
        >
          {data ? "Open Goodreads books ↗" : "Sign in to Goodreads ↗"}
        </a>
      </div>
      <Notice error={account.error || connect.error || discover.error} />

      {account.isPending && <Loading />}
      {data && (
        <>
          {data.warning && <p className="notice">{data.warning}</p>}
          <ShelfChoices
            key={data.user_id + data.discovered_at}
            provider="goodreads"
            choices={data.shelves}
            subscriptions={subscriptions.filter(
              (s) => s.subscription.provider === "goodreads",
            )}
            ready={ready}
            accountId={data.user_id}
            initial={data.selected || "to-read"}
            actions={
              <button
                disabled={discover.isPending || connect.isPending}
                onClick={() => discover.mutate()}
              >
                {discover.isPending ? (
                  "Looking for shelves…"
                ) : (
                  <>
                    <RefreshCw size={14} aria-hidden="true" /> Refresh shelves
                  </>
                )}
              </button>
            }
          />
        </>
      )}
      {!data &&
        subscriptions
          .filter((entry) => entry.subscription.provider === "goodreads")
          .map((entry) => <TrackedList key={entry.list_id} entry={entry} />)}
      <ConnectionEditor
        openLabel="Change Goodreads connection"
        connected={!!data}
      >
        <form
          className="editor reading-connect-form"
          onSubmit={(event) => {
            event.preventDefault();
            connect.mutate();
          }}
        >
          <label>
            Goodreads profile or books link
            <input
              value={profile}
              onChange={(event) => setProfile(event.target.value)}
              autoComplete="off"
              required
              maxLength={2000}
              placeholder="https://www.goodreads.com/user/show/…"
            />
          </label>
          <button
            className="primary"
            disabled={connect.isPending || discover.isPending}
          >
            {connect.isPending
              ? "Finding your shelves…"
              : data
                ? "Update Goodreads connection"
                : "Connect Goodreads"}
          </button>
        </form>
      </ConnectionEditor>
    </ReadingSection>
  );
}

function StoryGraphSetupGuide() {
  return (
    <details className="storygraph-setup">
      <summary>How to copy these values</summary>
      <ol>
        <li>
          <a
            href="https://app.thestorygraph.com/users/sign_in"
            target="_blank"
            rel="noopener noreferrer"
          >
            Sign in to StoryGraph ↗
          </a>{" "}
          and leave that tab open.
        </li>
        <li>
          On that tab, open the cookies for <code>app.thestorygraph.com</code>:
          <ul>
            <li>Chrome, Edge, or Brave: Inspect → Application → Cookies</li>
            <li>Firefox: Inspect → Storage → Cookies</li>
            <li>
              Safari: Settings → Advanced → Show features for web developers,
              then Develop → Show Web Inspector → Storage → Cookies
            </li>
          </ul>
        </li>
        <li>
          Copy the Value of <code>_storygraph_session</code> into the first
          field, and the Value of <code>remember_user_token</code> into the
          second. Extra spaces or a copied row are fine.
        </li>
      </ol>
    </details>
  );
}

function StoryGraphConnection({
  subscriptions,
  ready,
}: {
  subscriptions: Subscription[];
  ready: boolean;
}) {
  const cache = useQueryClient();
  const [sessionCookie, setSessionCookie] = useState("");
  const [rememberToken, setRememberToken] = useState("");
  const account = useQuery({
    queryKey: ["storygraph-account"],
    queryFn: async () =>
      result(await api.GET("/api/reading-accounts/storygraph")),
  });
  const connect = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/reading-accounts/storygraph", {
          body: {
            session_cookie: sessionCookie,
            remember_token: rememberToken,
          },
        }),
      ),
    onSuccess: (value) => {
      cache.setQueryData(["storygraph-account"], value);
      setSessionCookie("");
      setRememberToken("");
    },
  });
  const discover = useMutation({
    mutationFn: async () =>
      result(await api.POST("/api/reading-accounts/storygraph/discover")),
    onSuccess: (value) => cache.setQueryData(["storygraph-account"], value),
  });
  const disconnect = useMutation({
    mutationFn: async () => {
      result(await api.DELETE("/api/reading-accounts/storygraph"));
    },
    onSuccess: () => cache.setQueryData(["storygraph-account"], null),
  });
  const toRead = useTrackStoryGraphToRead();
  const data = account.data;
  return (
    <ReadingSection
      label="StoryGraph connection"
      title="StoryGraph"
      help={
        <SettingHelp label="StoryGraph">
          Paste the _storygraph_session and remember_user_token cookies from
          app.thestorygraph.com. They are a full StoryGraph login. Dewarr stores
          them encrypted and does not show them again. Shelves, tags, and lists
          you paste stay in sync with that session.
        </SettingHelp>
      }
      status={
        account.isPending
          ? "checking"
          : account.isError
            ? "unavailable"
            : data
              ? "connected"
              : "not-configured"
      }
    >
      <div className="reading-connection-tools">
        {data ? (
          <a href={data.profile_url} target="_blank" rel="noopener noreferrer">
            {data.username} ↗
          </a>
        ) : (
          <>
            <a
              className="reading-sign-in"
              href="https://app.thestorygraph.com/users/sign_in"
              target="_blank"
              rel="noopener noreferrer"
            >
              Sign in to StoryGraph ↗
            </a>
            <p className="muted">Then copy your session values below.</p>
          </>
        )}
      </div>
      <Notice
        error={
          account.error ||
          connect.error ||
          discover.error ||
          disconnect.error ||
          toRead.error
        }
      />
      {account.isPending && <Loading />}
      {data && (
        <>
          <ShelfChoices
            key={data.username + data.discovered_at}
            provider="storygraph"
            choices={data.shelves}
            subscriptions={subscriptions.filter(
              (s) => s.subscription.provider === "storygraph",
            )}
            ready={ready}
            accountId={data.username}
            initial="to-read"
            actions={
              <>
                <button
                  disabled={discover.isPending || connect.isPending}
                  onClick={() => discover.mutate()}
                >
                  {discover.isPending ? (
                    "Looking for lists…"
                  ) : (
                    <>
                      <RefreshCw size={14} aria-hidden="true" /> Refresh lists
                    </>
                  )}
                </button>
                <button
                  disabled={disconnect.isPending}
                  onClick={() => disconnect.mutate()}
                >
                  Disconnect
                </button>
              </>
            }
          />
          <p className="muted">
            Disconnecting removes the saved login. Lists you already follow stay
            here, and their checks wait until you connect again.
          </p>
        </>
      )}
      {!data &&
        subscriptions
          .filter((entry) => entry.subscription.provider === "storygraph")
          .map((entry) => <TrackedList key={entry.list_id} entry={entry} />)}
      <ConnectionEditor
        openLabel="Change StoryGraph connection"
        connected={!!data}
      >
        <form
          className="editor reading-connect-form reading-connect-form-pair"
          onSubmit={(event) => {
            event.preventDefault();
            connect.mutate();
          }}
        >
          <label>
            _storygraph_session
            <input
              value={sessionCookie}
              onChange={(event) => setSessionCookie(event.target.value)}
              autoComplete="off"
              required
              maxLength={4096}
              type="password"
            />
          </label>
          <label>
            remember_user_token
            <input
              value={rememberToken}
              onChange={(event) => setRememberToken(event.target.value)}
              autoComplete="off"
              required
              maxLength={4096}
              type="password"
            />
          </label>
          <button
            className="primary"
            disabled={connect.isPending || discover.isPending}
          >
            {connect.isPending
              ? "Finding your lists…"
              : data
                ? "Update StoryGraph connection"
                : "Connect StoryGraph"}
          </button>
        </form>
      </ConnectionEditor>
      <StoryGraphSetupGuide />
    </ReadingSection>
  );
}

function HardcoverConnection({
  subscriptions,
  ready,
  onConfigure,
}: {
  subscriptions: Subscription[];
  ready: boolean;
  onConfigure?: () => void;
}) {
  const account = useQuery({
    queryKey: ["metadata-account"],
    queryFn: async () => result(await api.GET("/api/metadata/account")),
  });
  return (
    <ReadingSection
      label="Hardcover connection"
      title="Hardcover"
      help={
        <SettingHelp label="Hardcover lists">
          Uses the connection in Metadata. Choose your own or followed lists to
          track here.
        </SettingHelp>
      }
      status={
        account.isPending
          ? "checking"
          : account.isError
            ? "unavailable"
            : account.data?.configured && !account.data.enabled
              ? "disabled"
              : account.data?.status || "not-configured"
      }
    >
      {account.data?.enabled && (
        <p className="reading-connection-note muted">
          Uses your Hardcover connection from Metadata. Choose the lists to keep
          in sync.
        </p>
      )}
      <Notice error={account.error} />
      {account.isPending && <Loading />}
      {account.data &&
        !account.data.enabled &&
        (onConfigure ? (
          <button type="button" onClick={onConfigure}>
            Set up Hardcover
          </button>
        ) : (
          <Link to="/settings#catalog">Set up Hardcover →</Link>
        ))}
      {account.data?.enabled ? (
        <HardcoverShelves subscriptions={subscriptions} ready={ready} />
      ) : (
        subscriptions
          .filter((entry) => entry.subscription.provider === "hardcover")
          .map((entry) => <TrackedList key={entry.list_id} entry={entry} />)
      )}
    </ReadingSection>
  );
}

function HardcoverShelves({
  subscriptions,
  ready,
}: {
  subscriptions: Subscription[];
  ready: boolean;
}) {
  const [mode, setMode] = useState<"owned" | "followed">("owned");
  const lists = usePagedQuery({
    queryKey: ["reading-hardcover-lists", mode],
    queryFn: async (cursor, signal) =>
      result(
        await api.GET("/api/metadata/hardcover-lists", {
          params: { query: { mode, cursor } },
          signal,
        }),
      ),
    retry: false,
    staleTime: 60000,
    initial: 0,
    next: (last) => last.next_cursor ?? undefined,
  });
  return (
    <>
      <label className="reading-list-filter">
        Show Hardcover lists
        <select
          value={mode}
          onChange={(event) => {
            setMode(event.target.value as typeof mode);
          }}
        >
          <option value="owned">My lists</option>
          <option value="followed">Lists I follow</option>
        </select>
      </label>
      <Notice error={lists.error} />
      {lists.isPending && <Loading />}
      {!lists.isPending && (
        <ShelfChoices
          actions={
            <div className="reading-refresh-actions">
              <button
                disabled={lists.isFetching}
                onClick={() => lists.refetch()}
              >
                {lists.isFetching ? (
                  "Checking lists…"
                ) : (
                  <>
                    <RefreshCw size={14} aria-hidden="true" /> Refresh lists
                  </>
                )}
              </button>
            </div>
          }
          key={mode}
          provider="hardcover"
          choices={lists.data?.items || []}
          subscriptions={subscriptions}
          ready={ready}
        />
      )}{" "}
      <InfiniteScroll query={lists} />
    </>
  );
}

function ShelfChoices({
  provider,
  choices,
  subscriptions,
  ready,
  initial,
  accountId,
  actions,
}: {
  provider: Provider;
  choices: Choice[];
  subscriptions: Subscription[];
  ready: boolean;
  initial?: string;
  accountId?: string;
  actions?: ReactNode;
}) {
  const cache = useQueryClient();
  const [selected, setSelected] = useState<string[]>(initial ? [initial] : []);
  const [message, setMessage] = useState("");
  const followed = new Set(
    subscriptions
      .filter(
        (s) =>
          s.subscription.provider === provider &&
          (!accountId || s.account_id === accountId),
      )
      .map((s) => s.external_id),
  );
  const available = choices.filter(
    (choice) => !followed.has(choice.external_id),
  );
  const pending = available.filter((choice) =>
    selected.includes(choice.external_id),
  );
  const follow = useMutation({
    mutationFn: async () => {
      setMessage("");
      let connected = 0;
      for (const choice of pending) {
        result(
          await api.POST("/api/reading-accounts/follow", {
            body: {
              provider,
              external_id: choice.external_id,
              interval_minutes: 60,
            },
          }),
        );
        connected++;
        setMessage(
          `${connected} ${connected === 1 ? "list connected" : "lists connected"}. First update queued; then checked every hour.`,
        );
      }
    },
    onSettled: async () => {
      await Promise.all([
        Promise.all(
          ["reading-subscriptions", "list-subscription"].map((key) =>
            cache.invalidateQueries({ queryKey: [key] }),
          ),
        ),
        cache.invalidateQueries({ queryKey: ["lists"] }),
        cache.invalidateQueries({ queryKey: ["discovery", "followed-lists"] }),
      ]);
    },
  });
  return (
    <div className="editor">
      <Notice error={follow.error} />
      {choices.length === 0 ? (
        <p>No lists found here.</p>
      ) : (
        <fieldset
          className="reading-choices"
          disabled={!ready || follow.isPending}
        >
          <legend>Choose lists to track</legend>
          {choices.map((choice) => {
            const tracked = subscriptions.find(
              (entry) =>
                entry.subscription.provider === provider &&
                entry.external_id === choice.external_id &&
                (!accountId || entry.account_id === accountId),
            );
            return tracked ? (
              <TrackedList
                key={choice.external_id}
                entry={tracked}
                count={choice.count}
              />
            ) : (
              <label className="reading-choice" key={choice.external_id}>
                <input
                  type="checkbox"
                  checked={selected.includes(choice.external_id)}
                  onChange={(event) =>
                    setSelected(
                      event.target.checked
                        ? [...selected, choice.external_id]
                        : selected.filter((id) => id !== choice.external_id),
                    )
                  }
                />
                <span className="reading-list-name">{choice.name}</span>
                {choice.count != null && (
                  <span className="reading-count">
                    {choice.count} {choice.count === 1 ? "book" : "books"}
                  </span>
                )}
              </label>
            );
          })}
        </fieldset>
      )}
      {subscriptions
        .filter(
          (entry) =>
            entry.subscription.provider === provider &&
            (!choices.some(
              (choice) => choice.external_id === entry.external_id,
            ) ||
              (!!accountId && entry.account_id !== accountId)),
        )
        .map((entry) => (
          <TrackedList key={entry.list_id} entry={entry} />
        ))}
      <div className="reading-list-footer">
        {available.length > 0 && (
          <div className="button-row">
            <button
              disabled={!ready || follow.isPending}
              onClick={() =>
                setSelected(
                  pending.length === available.length
                    ? []
                    : available.map((c) => c.external_id),
                )
              }
            >
              {pending.length === available.length
                ? "Deselect all"
                : "Select all"}
            </button>
            <button
              className="primary"
              disabled={!ready || !pending.length || follow.isPending}
              onClick={() => follow.mutate()}
            >
              {follow.isPending ? "Connecting lists…" : "Track selected lists"}
            </button>
          </div>
        )}
        <div className="reading-refresh-actions">{actions}</div>
      </div>
      {message && (
        <p role="status" className="success">
          {message}
        </p>
      )}
    </div>
  );
}

function TrackedList({
  entry,
  count,
}: {
  entry: Subscription;
  count?: number | null;
}) {
  const cache = useQueryClient();
  const [message, setMessage] = useState("");
  const sub = entry.subscription;
  const save = useMutation({
    mutationFn: async ({
      enabled,
      interval,
    }: {
      enabled: boolean;
      interval: number;
    }) =>
      result(
        await api.PUT("/api/lists/{list_id}/subscription", {
          params: { path: { list_id: entry.list_id } },
          body: {
            enabled,
            interval_minutes: interval,
            expected_generation: sub.generation,
          },
        }),
      ),
    onSuccess: () => setMessage("Tracking settings saved."),
    onSettled: () =>
      Promise.all(
        ["reading-subscriptions", "list-subscription"].map((key) =>
          cache.invalidateQueries({ queryKey: [key] }),
        ),
      ),
  });
  const refresh = useMutation({
    mutationFn: async () =>
      result(
        await api.POST("/api/lists/{list_id}/subscription/sync", {
          params: {
            path: { list_id: entry.list_id },
            header: { "idempotency-key": randomUUID() },
          },
        }),
      ),
    onSuccess: () => {
      setMessage(
        "Update queued. Your books will appear in the list when it finishes.",
      );
      cache.invalidateQueries({ queryKey: ["reading-subscriptions"] });
    },
  });
  const busy = save.isPending || refresh.isPending;
  return (
    <article
      className="reading-tracked"
      aria-label={`${entry.name} monitoring`}
    >
      <div className="reading-list-row">
        <Check
          className="reading-connected-icon"
          size={16}
          aria-hidden="true"
        />
        <Link className="reading-list-name" to={`/lists/${entry.list_id}`}>
          {entry.name}
        </Link>
        <span className="reading-count">
          {count ?? sub.observed_count}{" "}
          {(count ?? sub.observed_count) === 1 ? "book" : "books"}
        </span>
        <span
          className={`reading-status ${!sub.enabled ? "is-paused" : sub.state === "failed" ? "is-error" : ""}`}
        >
          {!sub.enabled ? (
            <Pause size={13} aria-hidden="true" />
          ) : sub.state === "failed" ? (
            <AlertCircle size={13} aria-hidden="true" />
          ) : ["queued", "running"].includes(sub.state) ? (
            <Clock3 size={13} aria-hidden="true" />
          ) : (
            <CircleCheck size={13} aria-hidden="true" />
          )}
          {!sub.enabled
            ? "Paused"
            : {
                idle: "Tracking",
                queued: "Update queued",
                running: "Updating",
                failed: "Needs attention",
              }[sub.state] || "Tracking"}
        </span>
        <div className="reading-row-actions">
          <select
            className="reading-interval"
            aria-label={`Check ${entry.name} for new books`}
            value={sub.interval_minutes}
            disabled={busy || !sub.enabled}
            onChange={(e) =>
              save.mutate({
                enabled: sub.enabled,
                interval: Number(e.target.value),
              })
            }
          >
            {![30, 60, 360, 1440].includes(sub.interval_minutes) && (
              <option value={sub.interval_minutes}>
                Every {sub.interval_minutes} min
              </option>
            )}
            <option value={30}>Every 30 min</option>
            <option value={60}>Hourly</option>
            <option value={360}>Every 6 hours</option>
            <option value={1440}>Daily</option>
          </select>
          <button
            type="button"
            className="reading-icon-button"
            aria-label={`Check for updates to ${entry.name}`}
            title="Check for updates"
            disabled={
              !sub.enabled || busy || ["queued", "running"].includes(sub.state)
            }
            onClick={() => refresh.mutate()}
          >
            <RefreshCw size={15} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="reading-icon-button"
            aria-label={`${sub.enabled ? "Pause" : "Resume"} tracking ${entry.name}`}
            title={sub.enabled ? "Pause tracking" : "Resume tracking"}
            disabled={busy}
            onClick={() =>
              save.mutate({
                enabled: !sub.enabled,
                interval: sub.interval_minutes,
              })
            }
          >
            {sub.enabled ? (
              <Pause size={15} aria-hidden="true" />
            ) : (
              <Check size={15} aria-hidden="true" />
            )}
          </button>
          <Link
            className="reading-more"
            aria-label={`Edit ${entry.name}`}
            title="List details"
            to={`/settings?list=${entry.list_id}#reading`}
          >
            <Ellipsis size={18} />
          </Link>
        </div>
      </div>
      {sub.state === "failed" && <p className="notice error">{sub.message}</p>}
      <Notice error={save.error || refresh.error} />
      {message && (
        <p role="status" className="muted">
          {message}
        </p>
      )}
    </article>
  );
}

function LocalLists({ subscriptions }: { subscriptions: Subscription[] }) {
  const lists = usePagedQuery({
    queryKey: ["lists", "reading-settings"],
    queryFn: async (offset, signal) =>
      result(
        await api.GET("/api/lists/page", {
          params: { query: { editable: true, offset, limit: 25 } },
          signal,
        }),
      ),
    initial: 0,
    next: (last, pages) => {
      const count = pages.reduce((n, p) => n + p.items.length, 0);
      return last.items.length && count < last.total ? count : undefined;
    },
  });
  const local =
    lists.data?.items.filter(
      (list) => !subscriptions.some((entry) => entry.list_id === list.id),
    ) || [];
  if (lists.isPending) return <Loading />;
  if (lists.error) return <Notice error={lists.error} />;
  if (!local.length && (lists.data?.total || 0) <= 25) return null;
  const count = local.length;
  return (
    <ReadingSection
      className="reading-local"
      label="Local lists"
      title="Local lists"
      status={
        <span className="connection-state">
          {count === 1 ? "1 list" : `${count} lists`}
        </span>
      }
    >
      <p className="muted">Lists you keep in this app.</p>
      {local.map((list) => (
        <div className="reading-list-row" key={list.id}>
          <Link to="/discover?view=yours" className="reading-list-name">
            {list.name}
          </Link>
          <span className="reading-count">{list.count} books</span>
          <Link
            className="reading-more"
            to={`/settings?list=${list.id}#reading`}
            title="List details"
            aria-label={`Edit ${list.name}`}
          >
            <Ellipsis size={18} />
          </Link>
        </div>
      ))}
      {!local.length && (
        <p className="muted">
          These lists are already shown with their reading accounts.
        </p>
      )}
      <InfiniteScroll query={lists} />
    </ReadingSection>
  );
}
