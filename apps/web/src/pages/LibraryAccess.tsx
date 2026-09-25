import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, ApiError, result } from "../api/client";
import type { components } from "../api/schema";
import { Loading, Notice } from "../components";
import {
  AccessChoices,
  AccessDialog,
  sameIds,
  useAccessLibraries,
} from "./AccessControls";

type Library = components["schemas"]["LibraryView"];
type Connection = components["schemas"]["ConnectionView"];
type User = components["schemas"]["UserView"];

export default function LibraryAccess({
  connections,
}: {
  connections: Connection[];
}) {
  const libraries = useAccessLibraries();
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: async () => result(await api.GET("/api/auth/users")),
  });
  const [editing, setEditing] = useState<Library | null>(null);
  const [saved, setSaved] = useState("");
  return (
    <section className="settings-block" aria-label="Library access">
      <div className="setting-subheading">
        <h3>Library access</h3>
        <Link className="settings-inline-link" to="/settings#accounts">
          Manage users →
        </Link>
      </div>
      <p className="muted">
        Choose who can use each library. You can also assign libraries when
        adding or editing a user. Administrators always have access.
      </p>
      <Notice error={libraries.error || accounts.error} />
      {saved && (
        <p className="success" role="status">
          {saved}
        </p>
      )}
      {libraries.isPending ? (
        <Loading />
      ) : libraries.data?.length ? (
        <div className="library-access-list">
          {libraries.data.map((library) => {
            const people = accounts.data?.filter(
              (user) =>
                user.role !== "admin" &&
                user.active !== false &&
                library.granted_user_ids.includes(user.id),
            );
            return (
              <article className="library-access-row" key={library.id}>
                <div className="library-access-info">
                  <h4>{library.name}</h4>
                  <span className="access-meta">
                    {
                      connections.find(
                        (item) => item.id === library.integration_id,
                      )?.name
                    }
                    {!library.accessible && " · Unavailable"}
                  </span>
                </div>
                <div className="library-access-summary">
                  {people ? (
                    <>
                      <span className={people.length ? "" : "access-attention"}>
                        {people.length
                          ? `${people.length} ${people.length === 1 ? "person has" : "people have"} access`
                          : "Administrators only"}
                      </span>
                      <small>
                        {people.length
                          ? people
                              .slice(0, 3)
                              .map((user) => user.display_name)
                              .join(", ") +
                            (people.length > 3
                              ? ` +${people.length - 3} more`
                              : "")
                          : "Add people to share this library"}
                      </small>
                    </>
                  ) : (
                    <span className="access-meta">
                      Access summary unavailable
                    </span>
                  )}
                </div>
                <button
                  type="button"
                  aria-label={`Edit access to ${library.name}`}
                  onClick={() => {
                    setSaved("");
                    setEditing(library);
                  }}
                >
                  Edit access
                </button>
              </article>
            );
          })}
        </div>
      ) : libraries.isSuccess ? (
        <p className="muted">Connect and sync a library to assign access.</p>
      ) : null}
      {editing && (
        <AccessEditor
          library={editing}
          close={() => setEditing(null)}
          onSaved={() => {
            setSaved(`Library access saved for ${editing.name}.`);
            setEditing(null);
          }}
        />
      )}
    </section>
  );
}

function AccessEditor({
  library,
  close,
  onSaved,
}: {
  library: Library;
  close: () => void;
  onSaved: () => void;
}) {
  const cache = useQueryClient();
  const accounts = useQuery({
    queryKey: ["accounts"],
    queryFn: async () => result(await api.GET("/api/auth/users")),
  });
  const [baseline, setBaseline] = useState(library);
  const [selected, setSelected] = useState(library.granted_user_ids);
  const changed = !sameIds(selected, baseline.granted_user_ids);
  const save = useMutation({
    mutationFn: async () =>
      result(
        await api.PUT("/api/library/libraries/{library_id}/grants", {
          params: { path: { library_id: library.id } },
          body: {
            user_ids: selected,
            expected_user_ids: baseline.granted_user_ids,
          },
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        cache.invalidateQueries({ queryKey: ["libraries"] }),
        cache.invalidateQueries({ queryKey: ["accounts"] }),
      ]);
      onSaved();
    },
  });
  const reload = useMutation({
    mutationFn: async () => {
      const [libraries, users] = await Promise.all([
        api
          .GET("/api/library/libraries", {
            params: { query: { include_disabled: true } },
          })
          .then(result),
        api.GET("/api/auth/users").then(result),
      ]);
      const current = libraries.find((item) => item.id === library.id);
      if (!current)
        throw new Error(
          "This library was removed. Close the editor and check its connection.",
        );
      cache.setQueryData(["libraries", "access"], libraries);
      cache.setQueryData(["accounts"], users);
      return current;
    },
    onSuccess: (current) => {
      setBaseline(current);
      setSelected(current.granted_user_ids);
      save.reset();
    },
  });
  const busy = save.isPending || reload.isPending;
  const conflict = save.error instanceof ApiError && save.error.status === 409;
  const people = accounts.data?.filter((user) => user.role !== "admin") ?? [];
  return (
    <AccessDialog
      title={`Library access: ${baseline.name}`}
      dirty={changed}
      busy={busy}
      close={close}
    >
      {(requestClose) => (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <p className="access-hint">
            Administrators always have access. Select the other people who can
            use this library.
          </p>
          {!baseline.accessible && (
            <p className="notice">
              This library is unavailable. Saved access applies when its
              connection is restored.
            </p>
          )}
          <Notice error={accounts.error || save.error || reload.error} />
          {conflict && (
            <div className="access-conflict">
              <p>Reloading replaces your draft with the latest saved access.</p>
              <button
                type="button"
                disabled={busy}
                onClick={() => reload.mutate()}
              >
                {reload.isPending ? "Reloading…" : "Reload saved settings"}
              </button>
            </div>
          )}
          {accounts.isPending && <Loading />}
          {accounts.isSuccess && (
            <AccessChoices
              title="People with access"
              hint="Roles control what each person can do. Library access controls the content they can use."
              searchLabel="Search people"
              selected={selected}
              onChange={setSelected}
              disabled={busy}
              options={people.map((user: User) => ({
                id: user.id,
                label: user.display_name,
                description: `@${user.username}`,
                badge:
                  user.active === false
                    ? "Disabled account"
                    : user.access_label || user.role,
                disabled:
                  user.active === false &&
                  !baseline.granted_user_ids.includes(user.id),
              }))}
            />
          )}
          {accounts.isSuccess && people.length === 0 && (
            <p className="muted">
              Add a user in Users & access, or have them sign in with Plex
              first.
            </p>
          )}
          <div className="access-form-actions">
            <button type="button" disabled={busy} onClick={requestClose}>
              Cancel
            </button>
            <button
              type="submit"
              className="primary"
              disabled={!changed || busy || !accounts.isSuccess || conflict}
            >
              {save.isPending ? "Saving…" : "Save library access"}
            </button>
          </div>
        </form>
      )}
    </AccessDialog>
  );
}
