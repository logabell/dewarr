import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronLeft, ChevronRight, X } from "lucide-react";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import BookCover from "../components/BookCover";
import { genreLabel } from "../components/DiscoveryCollections";
import FollowRelease, { basisLabel } from "../components/FollowRelease";
import QuickAdd from "../components/QuickAdd";
import BookLink from "../components/BookLink";

type Entry = components["schemas"]["ReleaseEntry"];
type Month = components["schemas"]["ReleaseMonth"];

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DAY_TITLES = 4;

function monthKey(date: Date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

function shiftMonth(month: string, delta: number) {
  const [year, mon] = month.split("-").map(Number);
  return monthKey(new Date(year, mon - 1 + delta, 1));
}

function monthTitle(month: string) {
  const [year, mon] = month.split("-").map(Number);
  return new Date(year, mon - 1, 1).toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
  });
}

function entryKey(entry: Entry) {
  return entry.work_id || `${entry.provider}:${entry.external_id}`;
}

function sameGenres(left: string[], right: string[]) {
  return (
    left.length === right.length && left.every((genre) => right.includes(genre))
  );
}

function GenreMenu({
  choices,
  selected,
  disabled,
  onToggle,
}: {
  choices: string[];
  selected: string[];
  disabled: boolean;
  onToggle: (genre: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const labels = selected.map(genreLabel);
  const summary =
    labels.length === 0
      ? "None selected"
      : labels.length <= 2
        ? labels.join(", ")
        : `${labels.length} selected`;
  useEffect(() => {
    if (!open) return;
    function onPointer(event: PointerEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      setOpen(false);
      trigger.current?.focus({ preventScroll: true });
    }
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="genre-menu" ref={root}>
      <button
        ref={trigger}
        type="button"
        className="genre-menu-trigger"
        aria-haspopup="true"
        aria-expanded={open}
        aria-controls="release-genre-menu"
        disabled={!choices.length}
        onClick={() => setOpen((current) => !current)}
      >
        <span className="genre-menu-label">Genres</span>
        <span className="genre-menu-value">{summary}</span>
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      {open && (
        <fieldset id="release-genre-menu" className="genre-menu-panel">
          <legend className="sr-only">Genres to track</legend>
          {choices.map((genre) => (
            <label key={genre}>
              <input
                type="checkbox"
                checked={selected.includes(genre)}
                disabled={disabled}
                onChange={() => onToggle(genre)}
              />
              {genreLabel(genre)}
            </label>
          ))}
        </fieldset>
      )}
    </div>
  );
}

function dayTitle(day: string) {
  const [year, mon, date] = day.split("-").map(Number);
  return new Date(year, mon - 1, date).toLocaleDateString(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
  });
}

function entryHref(entry: Entry) {
  if (entry.work_id) return `/books/${entry.work_id}`;
  if (entry.external_id)
    return `/discover/books/hardcover/${encodeURIComponent(entry.external_id)}`;
  return null;
}

function entryStatus(entry: Entry) {
  if (entry.in_library) return "In library";
  if (entry.state === "available") return "Available";
  if (entry.state === "wanted") return "Searching";
  if (entry.state === "waiting" || entry.followed) return "Waiting for release";
  if (entry.work_id) return "In your catalog";
  return "";
}

async function importHardcover(externalId: string) {
  const work = result(
    await api.POST("/api/metadata/books/{provider}/{external_id}/import", {
      params: { path: { provider: "hardcover", external_id: externalId } },
    }),
  );
  return work.id;
}

function daysInMonth(month: string) {
  const [year, mon] = month.split("-").map(Number);
  const count = new Date(year, mon, 0).getDate();
  const lead = (new Date(year, mon - 1, 1).getDay() + 6) % 7;
  const cells: Array<{ key: string; day: number } | null> = [
    ...Array.from({ length: lead }, () => null),
  ];
  for (let day = 1; day <= count; day += 1) {
    cells.push({
      key: `${year}-${String(mon).padStart(2, "0")}-${String(day).padStart(2, "0")}`,
      day,
    });
  }
  return cells;
}

function utcToday() {
  return new Date().toISOString().slice(0, 10);
}

function releaseIsAhead(day: string | null | undefined) {
  if (!day) return true;
  return day.slice(0, 10) > utcToday();
}

function alsoLabel(entry: Entry, section: string | null, selected: string[]) {
  if (!section) return "";
  const extra = selected.filter(
    (genre) => genre !== section && entry.genres?.includes(genre),
  );
  return extra.length ? `Also ${extra.map(genreLabel).join(", ")}` : "";
}

function genreSections(entries: Entry[], selected: string[]) {
  if (selected.length < 2) return [{ genre: null as string | null, entries }];
  const buckets = new Map(selected.map((genre) => [genre, [] as Entry[]]));
  const other: Entry[] = [];
  for (const entry of entries) {
    const match = selected.find((genre) => entry.genres?.includes(genre));
    if (match) buckets.get(match)?.push(entry);
    else other.push(entry);
  }
  const sections: { genre: string | null; entries: Entry[] }[] = [
    ...buckets.entries(),
  ]
    .filter(([, items]) => items.length > 0)
    .map(([genre, items]) => ({ genre, entries: items }));
  if (other.length) sections.push({ genre: null, entries: other });
  return sections;
}

function ReleaseCard({
  entry,
  canEdit,
  upcoming,
  note,
}: {
  entry: Entry;
  canEdit: boolean;
  upcoming: boolean;
  note?: string;
}) {
  const href = entryHref(entry);
  const status = entryStatus(entry);
  const canRequest =
    canEdit &&
    (entry.work_id || (entry.provider === "hardcover" && entry.external_id));
  const summary = (
    <>
      <BookCover title={entry.title} cover={entry.cover_url} actions={false} />
      <span className="release-day-card-copy">
        <span className="release-day-card-title">{entry.title}</span>
        <span>{entry.authors.join(", ") || "Author unknown"}</span>
        <span className="muted">{basisLabel(entry.basis)}</span>
        {note && <span className="muted">{note}</span>}
        {status && <span className="muted">{status}</span>}
      </span>
    </>
  );
  return (
    <article className="release-day-card">
      {href ? (
        <Link
          className="release-day-card-main"
          to={href}
          aria-label={`View ${entry.title}`}
        >
          {summary}
        </Link>
      ) : (
        <div className="release-day-card-main">{summary}</div>
      )}
      {canEdit && (upcoming || entry.followed || canRequest) && (
        <div className="release-day-card-actions">
          {(upcoming || entry.followed) && (
            <FollowRelease
              canEdit={canEdit}
              following={entry.followed}
              workId={entry.work_id}
              idleLabel={upcoming ? "Request on release day" : "Follow"}
              activeLabel={upcoming ? "Waiting for release" : "Following"}
              body={{
                work_id: entry.work_id,
                provider: entry.provider === "hardcover" ? "hardcover" : null,
                external_id: entry.external_id,
                title: entry.title,
                authors: entry.authors,
                cover_url: entry.cover_url,
                release_date: entry.release_date,
                basis: entry.basis,
              }}
            />
          )}
          {canRequest && !upcoming && (
            <QuickAdd
              workId={entry.work_id || undefined}
              resolveWork={
                entry.provider === "hardcover" && entry.external_id
                  ? () => importHardcover(entry.external_id!)
                  : undefined
              }
            />
          )}
        </div>
      )}
    </article>
  );
}

function DayPanel({
  dayKey,
  title,
  entries,
  genres,
  canEdit,
  upcoming,
  onClose,
}: {
  dayKey: string;
  title: string;
  entries: Entry[];
  genres: string[];
  canEdit: boolean;
  upcoming: boolean;
  onClose: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus({ preventScroll: true });
  }, [dayKey]);
  return (
    <aside
      id="release-day-panel"
      className="release-drawer"
      role="dialog"
      aria-modal="false"
      aria-labelledby="release-day-heading"
    >
      <header className="release-drawer-heading">
        <div>
          <h2 id="release-day-heading" tabIndex={-1} ref={heading}>
            {title}
          </h2>
          <p className="muted">
            {entries.length === 1 ? "1 release" : `${entries.length} releases`}
          </p>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="Close day"
          onClick={onClose}
        >
          <X size={16} aria-hidden="true" />
        </button>
      </header>
      {canEdit && upcoming && entries.length > 0 && (
        <p className="muted release-drawer-note">
          Request on release day waits until the book is out, then starts
          searching.
        </p>
      )}
      {entries.length === 0 ? (
        <p className="muted">Nothing is scheduled this day.</p>
      ) : (
        genreSections(entries, genres).map((section) => (
          <section key={section.genre ?? "other"}>
            {genres.length > 1 && (
              <h3 className="release-drawer-genre">
                {section.genre ? genreLabel(section.genre) : "Other"}
              </h3>
            )}
            <ul className="release-drawer-list">
              {section.entries.map((entry) => (
                <li key={entryKey(entry)}>
                  <ReleaseCard
                    entry={entry}
                    canEdit={canEdit}
                    upcoming={upcoming}
                    note={alsoLabel(entry, section.genre, genres)}
                  />
                </li>
              ))}
            </ul>
          </section>
        ))
      )}
    </aside>
  );
}

export function UpcomingShelf({ canEdit }: { canEdit: boolean }) {
  const query = useQuery({
    queryKey: ["release-upcoming"],
    queryFn: async () => result(await api.GET("/api/releases/upcoming")),
    retry: false,
  });
  const items = (query.data?.items || []).slice(0, 20);
  return (
    <section className="discovery-section" aria-label="Upcoming releases">
      <div className="section-heading discovery-heading">
        <h2>Upcoming releases</h2>
        <Link className="back-link" to="/discover?view=calendar">
          Calendar
        </Link>
      </div>
      <Notice error={query.error} />
      {query.isPending && <Loading />}
      {query.data?.warning && (
        <p className="notice" role="status">
          {query.data.warning}
        </p>
      )}
      {query.data?.status === "not-connected" && (
        <Link className="back-link" to="/settings#catalog">
          Connect Hardcover
        </Link>
      )}
      {query.data?.status === "ready" && items.length === 0 && (
        <p className="muted">
          {query.data.has_more
            ? "More of this month is on the calendar."
            : "No upcoming audiobooks for the genres you follow."}
        </p>
      )}
      {items.length > 0 && (
        <ul className="discovery-shelf" aria-label="Upcoming releases">
          {items.map((item) => (
            <li
              key={
                item.work?.id ||
                `${item.book.provider}:${item.book.external_id}`
              }
            >
              <BookLink
                className="book-card discovery-book"
                to={
                  item.work
                    ? `/books/${item.work.id}`
                    : item.book.external_id
                      ? `/discover/books/hardcover/${encodeURIComponent(item.book.external_id)}`
                      : `/search?q=${encodeURIComponent(item.book.title)}`
                }
                aria-label={`View ${item.book.title}`}
              >
                <BookCover
                  title={item.book.title}
                  cover={item.book.cover_url}
                  actions={false}
                />
                <h3 title={item.book.title}>{item.book.title}</h3>
                <p title={item.book.authors.join(", ")}>
                  {item.book.authors.join(", ") || "Author unknown"}
                </p>
                <p className="muted">{basisLabel(item.book.date_basis)}</p>
              </BookLink>
              <FollowRelease
                canEdit={canEdit}
                workId={item.work?.id}
                body={{
                  work_id: item.work?.id,
                  provider: "hardcover",
                  external_id: item.book.external_id,
                  title: item.book.title,
                  authors: item.book.authors,
                  cover_url: item.book.cover_url,
                  release_date: item.book.release_date,
                  basis: item.book.date_basis,
                }}
              />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default function ReleaseCalendar({ canEdit }: { canEdit: boolean }) {
  const cache = useQueryClient();
  const section = useRef<HTMLElement>(null);
  const heldScroll = useRef<number | null>(null);
  const writing = useRef(false);
  const desiredGenres = useRef<string[] | null>(null);
  const [month, setMonth] = useState(() => monthKey(new Date()));
  const [selectedDay, setSelectedDay] = useState<string | null>(null);
  const [draftGenres, setDraftGenres] = useState<string[] | null>(null);
  const remembered = useRef<Month | undefined>(undefined);
  const query = useQuery({
    queryKey: ["release-calendar", month],
    queryFn: async ({ signal }) =>
      result(
        await api.GET("/api/releases/calendar", {
          params: { query: { month } },
          signal,
        }),
      ),
    retry: false,
  });
  const saveGenres = useMutation({
    mutationFn: async (genres: string[]) =>
      result(await api.PUT("/api/releases/genres", { body: { genres } })),
    onSuccess: async () => {
      await cache.invalidateQueries({ queryKey: ["release-calendar"] });
      await cache.invalidateQueries({ queryKey: ["release-upcoming"] });
    },
  });
  const data: Month | undefined = query.data;
  if (data) remembered.current = data;
  const settled = data?.month === month;
  const genreSource = settled ? data : remembered.current;
  const items = settled ? data?.items || [] : [];
  const undated = settled ? data?.undated || [] : [];
  const byDay = useMemo(() => {
    const grouped = new Map<string, Entry[]>();
    for (const item of items) {
      if (!item.release_date) continue;
      grouped.set(item.release_date, [
        ...(grouped.get(item.release_date) || []),
        item,
      ]);
    }
    return grouped;
  }, [items]);
  const cells = daysInMonth(month);
  const selectedGenres = draftGenres ?? genreSource?.genres ?? [];
  useEffect(() => {
    if (
      draftGenres &&
      data?.genres &&
      sameGenres(draftGenres, data.genres) &&
      !writing.current &&
      !desiredGenres.current
    ) {
      setDraftGenres(null);
    }
  }, [data?.genres, draftGenres]);
  useLayoutEffect(() => {
    const scroller = document.scrollingElement;
    const done = settled || query.isError;
    if (done && section.current) section.current.style.minHeight = "";
    if (heldScroll.current != null && scroller) {
      scroller.scrollTop = heldScroll.current;
      if (done) heldScroll.current = null;
    }
  }, [month, query.isError, settled]);
  function changeMonth(delta: number) {
    const scroller = document.scrollingElement;
    if (section.current && scroller) {
      section.current.style.minHeight = `${section.current.offsetHeight}px`;
      heldScroll.current = scroller.scrollTop;
    }
    setSelectedDay(null);
    setMonth((current) => shiftMonth(current, delta));
  }
  function closeDay() {
    const key = selectedDay;
    setSelectedDay(null);
    if (!key) return;
    document
      .getElementById(`release-day-${key}`)
      ?.focus({ preventScroll: true });
  }
  useEffect(() => {
    if (!selectedDay) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") closeDay();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [selectedDay]);
  function toggleGenre(genre: string) {
    if (!canEdit) return;
    const current = desiredGenres.current ?? selectedGenres;
    const next = current.includes(genre)
      ? current.filter((item) => item !== genre)
      : [...current, genre];
    setDraftGenres(next);
    desiredGenres.current = next;
    if (writing.current) return;
    void flushGenres(genreSource?.genres ?? []);
  }
  async function flushGenres(fallback: string[]) {
    writing.current = true;
    try {
      while (desiredGenres.current) {
        const value = desiredGenres.current;
        desiredGenres.current = null;
        await saveGenres.mutateAsync(value);
        fallback = value;
      }
    } catch {
      desiredGenres.current = null;
      setDraftGenres(fallback);
    } finally {
      writing.current = false;
      if (desiredGenres.current) void flushGenres(fallback);
    }
  }
  const panelEntries =
    selectedDay === "undated"
      ? undated
      : selectedDay
        ? byDay.get(selectedDay) || []
        : [];
  return (
    <section
      ref={section}
      className="release-calendar"
      aria-label="Upcoming releases"
      aria-busy={!settled && !query.isError}
    >
      <div className="explore-view-heading">
        <div>
          <h2>Calendar</h2>
          <p className="muted">
            Audiobook release days for the genres you follow. A work date is
            labeled when the audiobook day is still unknown.
          </p>
        </div>
        <div className="release-toolbar">
          <GenreMenu
            choices={genreSource?.choices ?? []}
            selected={selectedGenres}
            disabled={!canEdit}
            onToggle={toggleGenre}
          />
          <div
            className="button-row release-month-nav"
            role="group"
            aria-label="Month"
          >
            <button
              type="button"
              className="icon-button"
              aria-label="Previous month"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => changeMonth(-1)}
            >
              <ChevronLeft size={16} />
            </button>
            <strong aria-live="polite">{monthTitle(month)}</strong>
            <button
              type="button"
              className="icon-button"
              aria-label="Next month"
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => changeMonth(1)}
            >
              <ChevronRight size={16} />
            </button>
          </div>
        </div>
      </div>
      <Notice error={query.error || saveGenres.error} />
      {settled && data?.warning && (
        <p className="notice" role="status">
          {data.warning}
        </p>
      )}
      {!settled && !query.isError && (
        <p className="muted" role="status">
          Loading {monthTitle(month)}.
        </p>
      )}
      {settled && !items.length && !undated.length && (
        <p className="muted">Nothing is scheduled this month.</p>
      )}
      <div className="release-weekdays" aria-hidden="true">
        {WEEKDAYS.map((day) => (
          <span key={day}>{day}</span>
        ))}
      </div>
      <div className="release-month">
        {cells.map((cell, index) => {
          if (!cell)
            return <div key={`pad-${index}`} className="release-pad" />;
          const entries = byDay.get(cell.key) || [];
          const visible =
            entries.length > DAY_TITLES ? DAY_TITLES - 1 : entries.length;
          const shown = entries.slice(0, visible);
          const extra = entries.length - shown.length;
          const open = selectedDay === cell.key;
          if (!entries.length) {
            return (
              <div key={cell.key} className="release-day">
                <span className="release-day-number">{cell.day}</span>
              </div>
            );
          }
          return (
            <button
              key={cell.key}
              id={`release-day-${cell.key}`}
              type="button"
              className="release-day"
              aria-expanded={open}
              aria-controls="release-day-panel"
              aria-label={`${dayTitle(cell.key)}, ${entries.length} ${entries.length === 1 ? "release" : "releases"}`}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() =>
                setSelectedDay((current) =>
                  current === cell.key ? null : cell.key,
                )
              }
            >
              <span className="release-day-number">{cell.day}</span>
              <span className="release-count">{entries.length}</span>
              <span className="release-marks">
                {shown.map((entry) => (
                  <span key={entryKey(entry)}>{entry.title}</span>
                ))}
                {extra > 0 && (
                  <span className="release-more">+{extra} more</span>
                )}
              </span>
            </button>
          );
        })}
      </div>
      {undated.length > 0 && (
        <button
          id="release-day-undated"
          type="button"
          className="text-button release-undated-open"
          aria-expanded={selectedDay === "undated"}
          aria-controls="release-day-panel"
          onClick={() =>
            setSelectedDay((current) =>
              current === "undated" ? null : "undated",
            )
          }
        >
          {undated.length} without a release day
        </button>
      )}
      {selectedDay && (
        <DayPanel
          dayKey={selectedDay}
          title={
            selectedDay === "undated"
              ? "Undated upcoming"
              : dayTitle(selectedDay)
          }
          entries={panelEntries}
          genres={settled ? data?.genres || [] : []}
          canEdit={canEdit}
          upcoming={selectedDay === "undated" || releaseIsAhead(selectedDay)}
          onClose={closeDay}
        />
      )}
    </section>
  );
}
