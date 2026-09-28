import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, result, setCsrf } from "../api/client";
import { Loading, Notice } from "../components";

const linkErrors: Record<string, string> = {
  denied: "Your identity provider did not approve the link.",
  mismatch: "This linking attempt expired or your session changed. Try again.",
  rejected:
    "The identity could not be linked. It may already belong to another account.",
  unavailable: "The identity provider could not be reached. Try again.",
  paused: "Account linking is paused during recovery review.",
  limited: "Too many attempts. Try again in ten minutes.",
};

export default function AccountSignIn() {
  const client = useQueryClient();
  const status = useQuery({
    queryKey: ["oidc-link"],
    queryFn: async () => result(await api.GET("/api/auth/oidc/link")),
  });
  const provider = useQuery({
    queryKey: ["oidc-status"],
    queryFn: async () => result(await api.GET("/api/auth/oidc")),
  });
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState(() => {
    const params = new URLSearchParams(window.location.search);
    const error = params.get("oidc_link_error");
    return error
      ? linkErrors[error] || "The account could not be linked."
      : params.has("oidc_linked")
        ? "Identity provider linked. You can now sign in with either method."
        : "";
  });
  const link = useMutation({
    mutationFn: async () =>
      result(await api.POST("/api/auth/oidc/link", { body: { password } })),
    onSuccess: (data) => {
      window.location.assign(data.authorization_url);
    },
    onSettled: () => setPassword(""),
  });
  const unlink = useMutation({
    mutationFn: async () =>
      result(await api.DELETE("/api/auth/oidc/link", { body: { password } })),
    onSuccess: (auth) => {
      setCsrf(auth.csrf_token);
      client.setQueryData(["session"], auth);
      setMessage(
        "Identity provider unlinked. Your local password remains available. Automatic account matching may link it again on a future provider sign-in.",
      );
      client.invalidateQueries({ queryKey: ["oidc-link"] });
    },
    onSettled: () => setPassword(""),
  });
  const busy = link.isPending || unlink.isPending;
  return (
    <div className="panel editor">
      <p>
        Link your existing account to an identity provider without losing your
        books, permissions, or local password.
      </p>
      {status.isPending && <Loading />}
      {message && <p role="status">{message}</p>}
      {status.data && (
        <>
          <p>
            Local password:{" "}
            {status.data.password_available ? "Available" : "Not configured"}
          </p>
          <p>
            Identity provider:{" "}
            {status.data.linked
              ? `Linked (${status.data.issuer})`
              : "Not linked"}
          </p>
          {!status.data.password_available ? (
            <p>
              Linking changes require a local password. Keep using your existing
              sign-in provider.
            </p>
          ) : status.data.linked || provider.data?.enabled ? (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                setMessage("");
                link.reset();
                unlink.reset();
                if (status.data.linked) unlink.mutate();
                else link.mutate();
              }}
            >
              <p>
                {status.data.linked
                  ? "Confirm your local password to unlink this provider."
                  : `Confirm your local password, then sign in with ${provider.data?.label || "your identity provider"}. Choose the identity you want to use for this account.`}
              </p>
              <label>
                Current password
                <input
                  type="password"
                  autoComplete="current-password"
                  required
                  maxLength={256}
                  value={password}
                  disabled={busy}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </label>
              <button className="primary" disabled={busy || !password}>
                {busy
                  ? "Please wait…"
                  : status.data.linked
                    ? "Unlink identity provider"
                    : "Link identity provider"}
              </button>
            </form>
          ) : (
            <p>
              An administrator must enable an identity provider in Users &amp;
              access first.
            </p>
          )}
        </>
      )}
      <Notice
        error={status.error || provider.error || link.error || unlink.error}
      />
    </div>
  );
}
