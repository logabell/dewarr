import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, Download } from "lucide-react";
import { Link } from "react-router-dom";
import { api, result } from "../api/client";
import { Notice } from "../components";
import { randomUUID } from "../randomUUID";

type Mode = "both" | "ebook" | "audio";
export default function ListDownloads({
  listId,
  source = "list",
  name,
  disabled,
}: {
  listId: string;
  source?: "list" | "goodreads" | "collection" | "hardcover" | "shelf";
  name: string;
  disabled: boolean;
}) {
  const cache = useQueryClient();
  const menu = useRef<HTMLDetailsElement>(null);
  // Keep keys across uncertain responses so retrying cannot duplicate a command.
  const keys = useRef(new Map<string, string>());
  const [progress, setProgress] = useState("");
  const download = useMutation({
    mutationFn: async (mode: Mode) => {
      const books = new Map<string, () => Promise<string>>();
      async function importBook(
        provider: "hardcover" | "openlibrary",
        external_id: string,
      ) {
        return result(
          await api.POST(
            "/api/metadata/books/{provider}/{external_id}/import",
            {
              params: { path: { provider, external_id } },
            },
          ),
        ).id;
      }
      if (source === "goodreads" || source === "collection") {
        let page = 1;
        while (true) {
          const batch = result(
            await api.GET("/api/discovery/collections/{collection_id}", {
              params: {
                path: { collection_id: listId },
                query: { page, full: true },
              },
            }),
          );
          for (const book of batch.items)
            books.set(book.external_id, async () => {
              if (book.subject && book.subject !== "work")
                throw new Error(
                  `Choose the recognized edition for ${book.title} before requesting.`,
                );
              if (book.work) return book.work.id;
              const resolved = result(
                await api.GET("/api/discovery/curation/{external_id}", {
                  params: { path: { external_id: book.external_id } },
                }),
              );
              if (resolved.entry.work) return resolved.entry.work.id;
              const match = resolved.match.book;
              if (!match)
                throw new Error(
                  resolved.match.reason ||
                    `Choose a matching edition for ${book.title}.`,
                );
              return importBook("hardcover", match.external_id);
            });
          if (!batch.has_more || !batch.items.length) break;
          page++;
        }
      } else if (source === "shelf") {
        if (listId !== "trending" && listId !== "new-releases")
          throw new Error("Unknown shelf");
        for (let page = 1; page <= 25; page++) {
          const batch = result(
            await api.GET("/api/discovery/hardcover/{shelf}", {
              params: { path: { shelf: listId }, query: { page } },
            }),
          );
          if (batch.status !== "ready")
            throw new Error(
              batch.warning || "Shelf unavailable. Try again later.",
            );
          for (const item of batch.items || []) {
            if (item.work) books.set(item.work.id, async () => item.work!.id);
            else if (item.book.external_id)
              books.set(item.book.external_id, async () =>
                importBook("hardcover", item.book.external_id!),
              );
          }
          if (!batch.has_more || !batch.items?.length) break;
        }
      } else if (source === "hardcover") {
        let cursor = 0;
        while (true) {
          const batch = result(
            await api.GET("/api/discovery/lists/{external_id}", {
              params: { path: { external_id: listId }, query: { cursor } },
            }),
          );
          for (const item of batch.items) {
            const id = item.book.external_id;
            if (item.work) books.set(item.work.id, async () => item.work!.id);
            else if (id) books.set(id, async () => importBook("hardcover", id));
          }
          if (batch.next_cursor == null || batch.next_cursor <= cursor) break;
          cursor = batch.next_cursor;
        }
      } else {
        let offset = 0;
        let revision: string | undefined;
        while (true) {
          const page = result(
            await api.GET("/api/lists/{list_id}", {
              params: {
                path: { list_id: listId },
                query: { offset, limit: 100, expected_revision: revision },
              },
            }),
          );
          revision = page.content_revision;
          page.items.forEach((work) => books.set(work.id, async () => work.id));
          offset += page.items.length;
          if (offset >= page.count || !page.items.length) break;
        }
      }
      const seen = new Set<string>();
      let queued = 0,
        available = 0,
        held = 0,
        failed = 0;
      const errors: string[] = [];
      for (const [index, resolve] of [...books.values()].entries()) {
        setProgress(`Checking ${index + 1} of ${books.size} books…`);
        try {
          const id = await resolve();
          if (seen.has(id)) continue;
          seen.add(id);
          const command = `${id}:${mode}`;
          const key = keys.current.get(command) || randomUUID();
          keys.current.set(command, key);
          const receipt = result(
            await api.POST("/api/requests/quick-add", {
              params: { header: { "idempotency-key": key } },
              body: { work_id: id, specification: { mode } },
            }),
          );
          cache.setQueryData(["quick-add", id], receipt);
          if (["queued", "running"].includes(receipt.status)) queued++;
          else if (receipt.status === "completed") available++;
          else held++;
          keys.current.delete(command);
        } catch (error) {
          failed++;
          const message =
            error instanceof Error
              ? error.message
              : "Could not request this book.";
          if (!errors.includes(message)) errors.push(message);
        }
      }
      const summary = books.size
        ? [
            `${queued} queued`,
            `${available} already available or requested`,
            ...(held ? [`${held} need attention`] : []),
            ...(failed ? [`${failed} failed`] : []),
          ].join(" · ")
        : "This list has no books yet.";
      setProgress(`${summary}${errors.length ? `. ${errors[0]}` : ""}`);
      for (const key of ["requests", "activity", "downloads"])
        void cache.invalidateQueries({ queryKey: [key] });
    },
    onError: () => setProgress(""),
  });
  function choose(mode: Mode) {
    if (menu.current) menu.current.open = false;
    setProgress("Checking your list…");
    download.mutate(mode);
  }
  const busy = disabled || download.isPending;
  return (
    <div className="quick-add list-downloads">
      <div className="quick-add-split">
        <button
          className="primary"
          disabled={busy}
          onClick={() => choose("both")}
          title="Download missing ebooks and audiobooks using your saved preferences"
        >
          <Download size={16} />
          {download.isPending ? "Checking list…" : "Download all missing"}
        </button>
        <details
          ref={menu}
          className="quick-add-menu"
          onBlur={(e) => {
            if (!e.currentTarget.contains(e.relatedTarget))
              e.currentTarget.open = false;
          }}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.currentTarget.open = false;
              e.currentTarget.querySelector("summary")?.focus();
            }
          }}
        >
          <summary
            aria-label={`Download format for ${name}`}
            title="Choose a format"
          >
            <ChevronDown size={16} />
          </summary>
          <div className="quick-add-options">
            <button disabled={busy} onClick={() => choose("both")}>
              Both
            </button>
            <button disabled={busy} onClick={() => choose("ebook")}>
              Ebook
            </button>
            <button disabled={busy} onClick={() => choose("audio")}>
              Audiobook
            </button>
            <small>Uses your saved format priorities.</small>
          </div>
        </details>
      </div>
      <Notice error={download.error} />
      {progress && (
        <div className="list-download-status" role="status">
          {progress}
          {!download.isPending && (
            <>
              {" "}
              <Link to="/requests">View downloads</Link>
            </>
          )}
        </div>
      )}
    </div>
  );
}
