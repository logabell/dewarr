import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";

type Pace = components["schemas"]["BackgroundWork"]["pace"];

const paces: { id: Pace; title: string; description: string }[] = [
  {
    id: "normal",
    title: "Normal",
    description: "Every background job runs on its usual schedule.",
  },
  {
    id: "relaxed",
    title: "Relaxed",
    description:
      "Background jobs run 5 times less often. Downloads, imports and list checks are noticed a little later.",
  },
  {
    id: "light",
    title: "Light",
    description:
      "Background jobs run 20 times less often. Good for a small server that also does other work.",
  },
  {
    id: "minimal",
    title: "Minimal",
    description:
      "Background jobs run 50 times less often. Finished downloads can take up to an hour to import.",
  },
];

export default function BackgroundWork() {
  const cache = useQueryClient();
  const setting = useQuery({
    queryKey: ["background-work"],
    queryFn: async () => result(await api.GET("/api/background-work")),
  });
  const [draft, setDraft] = useState<Pace | null>(null);
  const pace = draft ?? setting.data?.pace ?? "normal";
  const save = useMutation({
    mutationFn: async (submitted: Pace) =>
      result(
        await api.PUT("/api/background-work", { body: { pace: submitted } }),
      ),
    onSuccess: (saved, submitted) => {
      setDraft((current) => (current === submitted ? null : current));
      cache.setQueryData(["background-work"], saved);
    },
  });
  return (
    <section className="panel editor" aria-labelledby="background-work-title">
      <h2 id="background-work-title">Background work</h2>
      <p className="muted">
        Dewarr checks downloads, imports, lists and connections on a schedule.
        On a small or shared server, a slower pace saves CPU and database work.
        Searching and requesting books are not affected.
      </p>
      <p className="access-hint">
        New in this version. Normal keeps the schedule Dewarr has always used.
      </p>
      {setting.isPending ? (
        <Loading />
      ) : setting.data ? (
        <form
          className="editor"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate(pace);
          }}
        >
          <fieldset className="access-choice-list">
            <legend>How often background jobs run</legend>
            {paces.map((option) => (
              <label key={option.id} className="check-label">
                <input
                  type="radio"
                  name="background-pace"
                  value={option.id}
                  checked={pace === option.id}
                  onChange={() => setDraft(option.id)}
                />
                <span>
                  <strong>{option.title}</strong>
                  <span className="access-hint"> {option.description}</span>
                </span>
              </label>
            ))}
          </fieldset>
          <button className="primary" disabled={save.isPending}>
            {save.isPending ? "Saving…" : "Save background work"}
          </button>
          {save.isSuccess && (
            <p role="status">
              Background work saved. It applies within a minute.
            </p>
          )}
        </form>
      ) : null}
      <Notice error={setting.error || save.error} />
    </section>
  );
}
