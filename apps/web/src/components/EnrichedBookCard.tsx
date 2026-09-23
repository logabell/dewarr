import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, result, type Work } from "../api/client";
import type { components } from "../api/schema";
import { BookCard } from "../components";

type ReaderMatch = components["schemas"]["ReaderMatch"];
type Pending = {
  workId: string;
  signal: AbortSignal;
  done: (value: ReaderMatch) => void;
  fail: (error: unknown) => void;
};

// Shelf pages can contain hundreds of books. Only resolve visible cards, a few
// per request, and share the result with the book detail page without
// importing a catalog match.
const BATCH = 8;
let active = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
const queue: Pending[] = [];

function schedule() {
  if (timer || active >= 2 || !queue.length) return;
  timer = setTimeout(() => {
    timer = undefined;
    void send();
  }, 50);
}

async function send() {
  const batch = queue.splice(0, BATCH).filter((item) => !item.signal.aborted);
  if (!batch.length) return schedule();
  active++;
  try {
    const { results } = result(
      await api.POST("/api/metadata/reader-matches", {
        body: { work_ids: [...new Set(batch.map((item) => item.workId))] },
      }),
    );
    for (const item of batch)
      item.done(results[item.workId] ?? { status: "unmatched" });
  } catch (error) {
    for (const item of batch) item.fail(error);
  } finally {
    active--;
    schedule();
  }
}

function resolve(workId: string, signal: AbortSignal) {
  signal.throwIfAborted();
  return new Promise<ReaderMatch>((done, fail) => {
    queue.push({ workId, signal, done, fail });
    schedule();
  });
}

export default function EnrichedBookCard({ work }: { work: Work }) {
  const node = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const needsMetadata = work.provisional || !work.cover_url;
  const account = useQuery({
    queryKey: ["metadata-account"],
    queryFn: async () => result(await api.GET("/api/metadata/account")),
    enabled: needsMetadata,
  });
  useEffect(() => {
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) {
        setVisible(true);
        observer.disconnect();
      }
    });
    if (node.current) observer.observe(node.current);
    return () => observer.disconnect();
  }, []);
  const match = useQuery({
    queryKey: ["work-reader-match", work.id],
    queryFn: ({ signal }) => resolve(work.id, signal),
    enabled: visible && needsMetadata && !!account.data?.enabled,
    staleTime: 300_000,
    retry: false,
  });
  const book = account.data?.enabled ? match.data?.book : undefined;
  return (
    <div ref={node}>
      <BookCard
        work={
          book
            ? {
                ...work,
                title: book.title,
                authors: book.authors?.length ? book.authors : work.authors,
                description: book.description || work.description,
                cover_url: work.cover_url || book.cover_url || null,
              }
            : work
        }
      />
    </div>
  );
}
