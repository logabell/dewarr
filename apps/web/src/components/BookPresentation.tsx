import { useState, type ReactNode, type Ref } from "react";
import { Link } from "react-router-dom";
import type { Work } from "../api/client";
import type { components } from "../api/schema";
import BookCover from "./BookCover";
import { languageName } from "./LanguageSelect";
import { HardcoverRating } from "./BookReaderDetails";

type ReaderDetails = components["schemas"]["ReaderDetails"];

export function releaseIsAhead(
  releaseDate?: string | null,
  year?: number | null,
) {
  const today = new Date().toISOString().slice(0, 10);
  const dated =
    releaseDate && releaseDate.length >= 10 ? releaseDate.slice(0, 10) : null;
  if (dated) return dated > today;
  return typeof year === "number" && year > Number(today.slice(0, 4));
}

function publicationFact(releaseDate?: string | null, year?: number | null) {
  const dated =
    releaseDate && releaseDate.length >= 10 ? releaseDate.slice(0, 10) : null;
  const ahead = releaseIsAhead(releaseDate, year);
  return {
    label: ahead ? "Release date" : "First published",
    showDate: !!dated && (ahead || !year || dated.startsWith(String(year))),
  };
}

export function BookHero({
  title,
  providerBook,
  authors,
  work,
  cover,
  caption,
  eyebrow,
  series,
  details,
  year,
  language,
  editions,
  editionsMore,
  headingRef,
  children,
}: {
  providerBook?: { provider: string; external_id: string };
  title: string;
  authors: string[];
  work?: Work | null;
  cover?: string | null;
  caption: string;
  eyebrow: string;
  series?: ReactNode;
  details?: ReaderDetails;
  year?: number | null;
  language?: string | null;
  editions?: number;
  editionsMore?: boolean;
  headingRef?: Ref<HTMLHeadingElement>;
  children: ReactNode;
}) {
  const [expanded, setExpanded] = useState(false);
  const longTitle = title.length > 90;
  const colon = title.indexOf(": ");
  const splitTitle = longTitle && colon > 5 && colon < 90;
  const headline = splitTitle ? title.slice(0, colon) : title;
  const subtitle = splitTitle ? title.slice(colon + 2) : null;
  const published = publicationFact(details?.release_date, year);
  return (
    <header className="reader-hero book-detail-hero">
      <div className="reader-cover-wrap">
        <BookCover
          actions={false}
          title={title}
          providerBook={providerBook}
          cover={cover}
          work={work}
          rating={details?.rating}
        />
        <span className="reader-cover-caption">{caption}</span>
      </div>
      <div className="reader-hero-copy">
        <p className="eyebrow">{eyebrow}</p>
        {series}
        <h1
          ref={headingRef}
          tabIndex={-1}
          className={`${longTitle && !splitTitle ? "reader-title-long" : ""} ${expanded ? "is-expanded" : ""}`}
          title={title}
          aria-label={title}
        >
          {headline}
        </h1>
        {subtitle && (
          <p className={`reader-subtitle ${expanded ? "is-expanded" : ""}`}>
            {subtitle}
          </p>
        )}
        {longTitle && (
          <button
            className="reader-title-toggle"
            aria-expanded={expanded}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? "Show less" : "Show full title"}
          </button>
        )}
        <p className="author-line">
          {authors.length
            ? authors.map((name, index) => (
                <span key={`${name}:${index}`}>
                  {index > 0 ? ", " : ""}
                  <Link
                    to={
                      details?.authors?.find(
                        (author) =>
                          author.name.toLocaleLowerCase() ===
                          name.toLocaleLowerCase(),
                      )
                        ? `/authors/hardcover/${details.authors.find((author) => author.name.toLocaleLowerCase() === name.toLocaleLowerCase())!.external_id}`
                        : `/search?q=${encodeURIComponent(name)}`
                    }
                  >
                    {name}
                  </Link>
                </span>
              ))
            : "Author unknown"}
        </p>
        {work?.availability.audio && (
          <p className="narrator-line">
            {work.availability.primary_audio_narrators?.length
              ? `Narrated by ${work.availability.primary_audio_narrators.join(", ")}`
              : "Narrator not supplied"}
            {(work.availability.audio_versions || 0) > 1 && (
              <Link
                className="version-count"
                to={`/books/${work.id}?tab=library&format=audio`}
                aria-label="View other audiobook recordings"
              >
                +{work.availability.audio_versions! - 1}
              </Link>
            )}
          </p>
        )}
        {!!work?.availability.parts_total && (
          <p className="narrator-line">
            {work.availability.parts_owned} of {work.availability.parts_total}{" "}
            {work.availability.parts_medium === "ebook" ? "ebook" : "audiobook"}{" "}
            parts in your library ·{" "}
            <Link to={`/books/${work.id}?tab=library`}>see which</Link>
          </p>
        )}
        <HardcoverRating details={details} />
        <dl className="reader-facts">
          {(year || details?.release_date) && (
            <div>
              <dt>{published.label}</dt>
              <dd>
                {published.showDate && details?.release_date ? (
                  <time dateTime={details.release_date}>
                    {new Date(details.release_date).toLocaleDateString(
                      undefined,
                      {
                        month: "short",
                        day: "numeric",
                        year: "numeric",
                        timeZone: "UTC",
                      },
                    )}
                  </time>
                ) : (
                  year
                )}
              </dd>
            </div>
          )}
          {!!details?.pages && (
            <div>
              <dt>Pages</dt>
              <dd>{details.pages.toLocaleString()}</dd>
            </div>
          )}
          {!!details?.audio_seconds && (
            <div>
              <dt>Audio length</dt>
              <dd>
                {Math.floor(details.audio_seconds / 3600)}h{" "}
                {Math.floor((details.audio_seconds % 3600) / 60)}m
              </dd>
            </div>
          )}
          {editions !== undefined && (
            <div>
              <dt>Editions</dt>
              <dd>
                {editions}
                {editionsMore ? "+" : ""}
              </dd>
            </div>
          )}
          {language && (
            <div>
              <dt>Language</dt>
              <dd>{languageName(language)}</dd>
            </div>
          )}
        </dl>
        {children}
      </div>
    </header>
  );
}

export function BookOverview({
  description,
  subjects = [],
}: {
  description?: string | null;
  subjects?: string[];
}) {
  return (
    <section
      id="overview"
      className="reader-section"
      aria-labelledby="overview-heading"
    >
      <p className="eyebrow">BETWEEN THE COVERS</p>
      <h2 id="overview-heading">About this book</h2>
      <p className="reader-prose reader-synopsis">
        {description || "No synopsis is available for this book yet."}
      </p>
      {!!subjects.length && (
        <div className="status-row">
          {subjects.slice(0, 12).map((subject) => (
            <span className="status" key={subject}>
              {subject}
            </span>
          ))}
        </div>
      )}
    </section>
  );
}
