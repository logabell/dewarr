# OpenID Connect

Dewarr can sign people in with one OpenID Connect provider. Authentik, Pocket ID, and Authelia all use this same setup. Local usernames and passwords stay available, including the first administrator account.

Create that first account in the browser before enabling the provider. Then open **Settings → Users & access**.

`PUBLIC_URL` must be the exact address in the browser, including `https://` when you use TLS. The redirect URL shown on the settings page is:

`https://books.example.com/api/auth/oidc/callback`

Copy it into the provider. Dewarr uses authorization code with PKCE (`S256`) and `client_secret_basic`. Correcting the issuer path keeps existing links when the scheme, host, and port stay the same. A different scheme, host, or port removes those links, so a reused subject cannot sign in as the old account.

## Link an existing account (including the first administrator)

Username matching deliberately does not attach an identity to an administrator. A matching username alone does not prove ownership of that account. This follows [OpenID Connect’s stable identifier rules](https://openid.net/specs/openid-connect-core-1_0.html#ClaimStability); explicit linking requires authentication of both accounts, consistent with [account-linking guidance](https://auth0.com/docs/manage-users/user-accounts/user-account-linking/link-user-accounts).

1. Sign in to Dewarr using your existing local username and password.
2. Enable and configure the provider in **Settings → Users & access** if needed.
3. Open **Settings → Sign-in**, enter your current local password, and choose **Link identity provider**.
4. Authenticate with the provider using the identity you want to attach.

The verified issuer and subject are linked to the same Dewarr account. Your account ID, books, library access, permissions, and local password are retained. This also works when automatic account matching and registration are off. An identity already linked to another account cannot be reassigned here.

You can unlink from **Settings → Sign-in** after confirming your local password. Unlinking ends the account's other sessions and pending linking attempts; your browser receives a new session. If automatic matching is enabled, a later provider sign-in may link the identity again. Accounts without a local password cannot unlink their only sign-in method.

Administrators can open **Settings → Users & access → Account details** to change a username or display name, or disable an account. Disabling ends its sessions and blocks sign-in while retaining owned data and audit history. Rename the account to free its old username. You cannot disable your own account or the last active administrator. Account deletion is not required to resolve a username collision or to link an existing administrator.

From another administrator account, **Account details → Sign-in and account removal** also offers OIDC/Plex unlinking and deletion of unused accounts. Type the displayed username to confirm. Unlinking requires a local password or another enabled, linked provider and ends all sessions and pending linking attempts. The account and its history remain. Provider matching and registration settings still govern future sign-ins. Deletion removes sign-in identities, sessions, and library grants, freeing the username. It rejects accounts with owned data, followed lists, preferences, or history; disable and rename those accounts instead. If the account changes during review, reload its details before retrying.

## Authentik

1. Create an application and choose the **OAuth2/OpenID Connect** provider. This is not the proxy provider.
2. Note the client ID and client secret.
3. Add the Dewarr redirect URL as a strict authorization redirect URI.
4. In Dewarr, paste the issuer URL (`https://authentik.example/application/o/<slug>`), choose **Discover endpoints**, enter the client ID and secret, and save.
5. Leave **Match existing accounts** on username or verified email if these people already have Dewarr accounts. Verified email matches only when the provider sets `email_verified` to true. Username matching does not attach an administrator account. Turn on **Create accounts on first sign-in** only when the provider should add new readers.
6. Optional: set the group claim to `groups` and enter the Authentik group names that should become administrators, members, or viewers. Leave **Group scope** empty unless Authentik releases that claim only when Dewarr requests a scope. A scope the provider does not offer rejects every provider sign-in.

A user's email must not be marked unverified. Authentik does that until the address is confirmed.

## Pocket ID

1. Create an OIDC client named Dewarr.
2. Set the callback URL to the Dewarr redirect URL.
3. Copy the client ID and client secret.
4. In Dewarr, paste the Pocket ID address (`https://id.example.com`) as the issuer, choose **Discover endpoints**, and save the client ID and secret.

Pocket ID puts group names in the `groups` claim. Set the group claim and the group scope to `groups`, and enter the Pocket ID group names in the role fields.

## Authelia

Add a confidential client that requires PKCE. Replace the secret with a hashed Authelia secret and the redirect with your Dewarr address:

```yaml
identity_providers:
  oidc:
    clients:
      - client_id: "dewarr"
        client_name: "Dewarr"
        client_secret: "$pbkdf2-sha512$..."
        public: false
        require_pkce: true
        pkce_challenge_method: "S256"
        redirect_uris:
          - "https://books.example.com/api/auth/oidc/callback"
        scopes:
          - "openid"
          - "profile"
          - "email"
          - "groups"
        response_types:
          - "code"
        grant_types:
          - "authorization_code"
        token_endpoint_auth_method: "client_secret_basic"
```

In Dewarr, the issuer is the Authelia address (`https://auth.example.com`). Discover fills the authorize, token, userinfo, and JWKS URLs. Set the group claim and the group scope to `groups`.

## Roles

New provider accounts are members unless a group mapping says otherwise. They cannot run list automation until an administrator allows it. Group mapping does not change the role of an account that still has a local password, and it will not remove the last active administrator. Signing out of Dewarr ends the Dewarr session only; the identity provider session can remain.

People created by the provider have no Dewarr password. Disabling the provider blocks their sign-in until you turn it back on or give them a local account.
