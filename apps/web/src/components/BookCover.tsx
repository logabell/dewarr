import { useState } from "react";
import { BookOpen, Check, Headphones, Star } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import QuickAdd from "./QuickAdd";
import { api, result, type Auth } from "../api/client";
import type { Work } from "../api/client";
import { useDisplayPreferences } from "../displayPreferences";
import { canStartDownload } from "../permissions";

type Props = {
  providerBook?: { provider: string; external_id: string };
  actions?: boolean;
  title: string;
  cover?: string | null;
  work?: Work | null;
  medium?: "any" | "ebook" | "audio";
  rating?: number | null;
};
export default function BookCover({
  title,
  providerBook,
  actions = true,
  cover,
  work,
  medium = "any",
  rating,
}: Props) {
  const display = useDisplayPreferences();
  const { data: session } = useQuery<Auth | null>({
    queryKey: ["session"],
    enabled: false,
  });
  const canDownload = canStartDownload(
    session?.user.permissions,
    session?.user.role,
    medium === "ebook" || medium === "audio" ? medium : undefined,
  );
  const canAdd =
    actions &&
    canDownload &&
    !!session &&
    !!(work || providerBook) &&
    !(work?.availability.ebook && work?.availability.audio);
  async function resolveWork() {
    let book = providerBook!;
    if (book.provider === "goodreads") {
      const resolved = result(
        await api.GET("/api/discovery/goodreads/{external_id}", {
          params: { path: { external_id: book.external_id } },
        }),
      );
      if (resolved.entry.work) return resolved.entry.work.id;
      if (!resolved.match.book)
        throw new Error(
          resolved.match.reason ||
            "Open this book to choose a matching edition.",
        );
      book = resolved.match.book;
    }
    if (book.provider !== "hardcover" && book.provider !== "openlibrary")
      throw new Error("Open this book to choose a supported catalog edition.");
    return result(
      await api.POST("/api/metadata/books/{provider}/{external_id}/import", {
        params: {
          path: { provider: book.provider, external_id: book.external_id },
        },
      }),
    ).id;
  }
  const [failed, setFailed] = useState<string[]>([]);
  const [nonPortrait, setNonPortrait] = useState<string[]>([]);
  const availability = work?.availability;
  const format = medium === "any" ? "ebook" : medium;
  const primaryVersion =
    format === "audio"
      ? availability?.primary_audio_version_id
      : availability?.primary_ebook_version_id;
  const libraryCover =
    availability?.owned && work
      ? `/api/catalog/works/${work.id}/cover?medium=${format}${primaryVersion ? `&version=${encodeURIComponent(primaryVersion)}` : ""}`
      : null;
  const shape =
    medium === "any"
      ? display.defaultShape
      : format === "audio"
        ? display.audioShape
        : display.ebookShape;
  const candidates = [libraryCover, work?.cover_url, cover].filter(
    (url): url is string => !!url && !failed.includes(url),
  );
  const src =
    shape === "portrait"
      ? candidates.find((url) => !nonPortrait.includes(url)) || candidates[0]
      : candidates[0];
  return (
    <div
      className={`book-cover cover-${shape}`}
      tabIndex={canAdd ? 0 : undefined}
      aria-label={canAdd ? `Download options for ${title}` : undefined}
    >
      {src ? (
        <img
          src={
            src.startsWith("https://")
              ? `/api/catalog/cover-image?url=${encodeURIComponent(src)}`
              : src
          }
          alt={`Cover of ${title}`}
          loading="lazy"
          referrerPolicy="no-referrer"
          onError={() => setFailed((old) => [...old, src])}
          onLoad={(event) => {
            const image = event.currentTarget;
            if (
              shape === "portrait" &&
              image.naturalWidth / image.naturalHeight > 0.85
            ) {
              setNonPortrait((old) =>
                old.includes(src) ? old : [...old, src],
              );
            }
          }}
        />
      ) : (
        <div className="type-cover">
          <BookOpen size={28} aria-hidden="true" />
          <span>{title}</span>
          <small>Cover unavailable</small>
        </div>
      )}
      {canAdd && (
        <QuickAdd
          key={
            work?.id || `${providerBook?.provider}:${providerBook?.external_id}`
          }
          workId={work?.id}
          resolveWork={resolveWork}
          coverFormats={{
            ebook: !!availability?.ebook,
            audio: !!availability?.audio,
          }}
        />
      )}
      {availability?.owned && (
        <span
          className="cover-owned"
          role="img"
          aria-label={
            availability.stale
              ? "In library · last known availability"
              : "In library"
          }
          title={
            availability.stale
              ? "In library · last known availability"
              : "In library"
          }
        >
          <Check size={15} aria-hidden="true" />
          <span className="sr-only">In library</span>
        </span>
      )}
      <div className="cover-formats">
        {(["ebook", "audio"] as const).map((kind) => {
          const owned = availability?.[kind];
          const versions =
            availability?.[
              kind === "audio" ? "audio_versions" : "ebook_versions"
            ] || 0;
          const stale =
            kind === "audio"
              ? availability?.audio_stale
              : availability?.ebook_stale;
          const partial =
            !owned && availability?.parts_medium === kind
              ? `${availability.parts_owned} of ${availability.parts_total} parts in library`
              : null;
          const label = `${kind === "audio" ? "Audiobook" : "Ebook"}: ${owned ? (stale ? "in library (last known)" : "in library") : partial || (work ? "not in library" : "library status unknown")}`;
          const Icon = kind === "audio" ? Headphones : BookOpen;
          return (
            <span
              key={kind}
              className={`cover-format ${owned ? "is-owned" : partial ? "is-partial" : "is-missing"} ${owned && versions > 1 ? "has-versions" : ""}`}
              role="img"
              aria-label={label}
              title={versions > 1 ? `${label} · ${versions} versions` : label}
            >
              <Icon size={15} aria-hidden="true" />
              {owned && versions > 1 && (
                <small aria-hidden="true">+{versions - 1}</small>
              )}
              {partial && (
                <small aria-hidden="true">
                  {availability!.parts_owned}/{availability!.parts_total}
                </small>
              )}
            </span>
          );
        })}
      </div>
      {rating != null && (
        <span className="cover-rating" title="Average rating">
          <Star size={12} aria-hidden="true" />
          {rating.toFixed(1)}
        </span>
      )}
    </div>
  );
}
