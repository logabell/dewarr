# Plex sign-in

Dewarr can offer **Sign in with Plex** next to local passwords. It stays off until an administrator turns it on. This is separate from [OpenID Connect](OIDC.md). A home can use either, or both.

Plex sign-in is for people who already have an account on one Plex server, including accounts invited with Wizarr. Each person gets their own Dewarr session, so a request stays attached to that person. Dewarr does not read the Plex library.

Create the first Dewarr administrator in the browser before enabling Plex. Then open **Settings → Users & access**.

`PUBLIC_URL` must be the exact address in the browser, including `https://` when you use TLS. Plex sends the browser back to:

`https://books.example.com/api/auth/plex/callback`

## Link the household server

1. Choose **Link a Plex server** and approve Dewarr in Plex.
2. Select the server your household uses.
3. Turn on **Enable Plex sign-in**.
4. Turn on **Create accounts on first sign-in** when someone who can access that server should get a Dewarr account the first time they sign in. New accounts are members or viewers, never administrators.
5. Save.

After a person signs in for the first time, open **Settings → Users & access** as
an administrator. Choose **Edit** next to their account, choose their role and
libraries, and **Save changes**. Both choices save together. Members can then see
those libraries' books and request content for them, subject to their role's
permissions. Existing members use the same workflow. The user list flags accounts
with **No library access** so incomplete setup is easy to find.

To manage several people for one library, use **Settings → Libraries → Library
access → Edit access**. Both editors manage the same grants. See
[Users and library access](USER-ACCESS.md) for local accounts, roles, and conflict
recovery.

Library access is separate from signing in and from default library choices.
Using the same email in Audiobookshelf does not copy its permissions into Dewarr.
If a member sees **Unavailable saved library**, check their library access and
the library connection before changing their defaults. Removing a library grant
removes access even when that library is still saved as a default.

People who can no longer access that server cannot sign in with Plex. Turning the setting off stops Plex sign-in and leaves the Dewarr accounts in place. An account created only through Plex has no local password. The first administrator keeps signing in with theirs.

Plex managed users who cannot approve a normal Plex sign-in are not supported. The Plex token from the approval is used once to check server access and is not stored.
