import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, result } from "../api/client";
import { Loading, Notice } from "../components";
import BookDialog from "../components/BookDialog";
import "./access-settings.css";

export function sameIds(left: string[], right: string[]) {
  return left.length === right.length && left.every((id) => right.includes(id));
}

export function useAccessLibraries(enabled = true) {
  return useQuery({
    queryKey: ["libraries", "access"],
    enabled,
    queryFn: async () =>
      result(
        await api.GET("/api/library/libraries", {
          params: { query: { include_disabled: true } },
        }),
      ),
  });
}

export function AccessDialog({
  title,
  dirty,
  busy,
  close,
  children,
}: {
  title: string;
  dirty: boolean;
  busy: boolean;
  close: () => void;
  children: (requestClose: () => void) => ReactNode;
}) {
  const [confirm, setConfirm] = useState(false);
  const keepEditing = useRef<HTMLButtonElement>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const requestClose = () => {
    if (busy) return;
    if (!dirty) return close();
    previousFocus.current = document.activeElement as HTMLElement | null;
    setConfirm(true);
  };
  useEffect(() => {
    if (confirm) keepEditing.current?.focus();
  }, [confirm]);
  useEffect(() => {
    if (!dirty) return;
    const prevent = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", prevent);
    return () => window.removeEventListener("beforeunload", prevent);
  }, [dirty]);
  return (
    <BookDialog
      title={title}
      close={requestClose}
      className="access-dialog access-settings-dialog"
    >
      {confirm ? (
        <section className="access-discard" aria-label="Unsaved changes">
          <h3>Discard unsaved changes?</h3>
          <p>Your changes have not been saved.</p>
          <div className="access-form-actions">
            <button type="button" onClick={close}>
              Discard changes
            </button>
            <button
              type="button"
              className="primary"
              ref={keepEditing}
              onClick={() => {
                setConfirm(false);
                requestAnimationFrame(() => previousFocus.current?.focus());
              }}
            >
              Keep editing
            </button>
          </div>
        </section>
      ) : null}
      <div hidden={confirm}>{children(requestClose)}</div>
    </BookDialog>
  );
}

type Choice = {
  id: string;
  label: string;
  description?: string;
  badge?: string;
  disabled?: boolean;
};
export function AccessChoices({
  title,
  hint,
  options,
  selected,
  onChange,
  disabled,
  searchLabel,
}: {
  title: string;
  hint: string;
  options: Choice[];
  selected: string[];
  onChange: (ids: string[]) => void;
  disabled?: boolean;
  searchLabel: string;
}) {
  const hintId = useId();
  const [search, setSearch] = useState("");
  const needle = search.trim().toLocaleLowerCase();
  const shown = options.filter((item) =>
    `${item.label} ${item.description ?? ""} ${item.badge ?? ""}`
      .toLocaleLowerCase()
      .includes(needle),
  );
  return (
    <fieldset
      className="access-choices"
      disabled={disabled}
      aria-describedby={hintId}
    >
      <legend>{title}</legend>
      <p className="access-hint" id={hintId}>
        {hint}
      </p>
      {options.length > 6 && (
        <label className="access-search">
          <span className="sr-only">{searchLabel}</span>
          <input
            type="search"
            name="access-search"
            autoComplete="off"
            placeholder={`${searchLabel}…`}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
      )}
      <div className="access-choice-list">
        {shown.map((item) => (
          <label className="access-choice" key={item.id}>
            <input
              type="checkbox"
              name={title}
              value={item.id}
              checked={selected.includes(item.id)}
              disabled={item.disabled}
              onChange={(event) =>
                onChange(
                  event.target.checked
                    ? [...selected, item.id]
                    : selected.filter((id) => id !== item.id),
                )
              }
            />
            <span className="access-choice-text">
              <strong>{item.label}</strong>
              {item.description && <small>{item.description}</small>}
            </span>
            {item.badge && <span className="access-badge">{item.badge}</span>}
          </label>
        ))}
        {shown.length === 0 && (
          <p className="muted">
            {needle
              ? "No matches. Try another search."
              : "No choices available yet."}
          </p>
        )}
      </div>
    </fieldset>
  );
}

export function UserLibraries({
  query,
  selected,
  baseline,
  onChange,
  administrator,
  accountDisabled = false,
  busy = false,
}: {
  query: ReturnType<typeof useAccessLibraries>;
  selected: string[];
  baseline: string[];
  onChange: (ids: string[]) => void;
  administrator: boolean;
  accountDisabled?: boolean;
  busy?: boolean;
}) {
  const hidden = selected.filter(
    (id) => !query.data?.some((library) => library.id === id),
  ).length;
  return (
    <section className="user-library-choices" aria-label="Library access">
      <Notice error={query.error} />
      {query.isError && (
        <button type="button" onClick={() => query.refetch()}>
          Retry library choices
        </button>
      )}
      {administrator ? (
        <>
          <h3>Library access</h3>
          <p className="access-hint">
            Administrators have access to all libraries, including libraries
            connected later. Saved individual choices are retained for a future
            role change.
          </p>
        </>
      ) : (
        <>
          {query.isPending && <Loading />}
          {query.isSuccess && (
            <>
              <AccessChoices
                title="Libraries"
                hint="Choose which libraries this person can use. Their role controls what they can do."
                selected={selected}
                onChange={onChange}
                disabled={busy}
                searchLabel="Search libraries"
                options={query.data.map((library) => ({
                  id: library.id,
                  label: library.name,
                  description: library.accessible
                    ? undefined
                    : "Access applies when the connection is available.",
                  badge: library.accessible ? undefined : "Unavailable",
                  disabled: accountDisabled && !baseline.includes(library.id),
                }))}
              />
              <p
                className={`access-selection-summary ${selected.length ? "" : "access-attention"}`}
                role="status"
              >
                {selected.length
                  ? `${selected.length} ${selected.length === 1 ? "library" : "libraries"} selected`
                  : "No library access — select a library to complete access setup."}
              </p>
              {query.data.length === 0 && (
                <p className="muted">
                  Connect a library in Settings → Libraries. You can save this
                  account and assign access later.
                </p>
              )}
              {hidden > 0 && (
                <p className="muted">
                  {hidden} saved {hidden === 1 ? "choice is" : "choices are"}{" "}
                  unavailable and will be kept.
                </p>
              )}
            </>
          )}
        </>
      )}
    </section>
  );
}
