# Reading accounts

Onboarding and **Settings → Reading accounts** share the same connections and controls. Members and administrators can connect and follow lists; viewer accounts remain read-only.

## Goodreads

Open **My Goodreads books**, sign in on Goodreads if needed, and paste the resulting My Books or profile address. Numeric user IDs and shelf RSS links also work. The app validates the feed, discovers shelf names/counts from the public profile (including `tag=` links and empty shelves), and supplements discovery with shelf names present in the feed. If profile HTML is unavailable, the UI explicitly labels discovery as partial. A pasted shelf is selected by default; otherwise Want to Read is selected.

Goodreads data travels one way into local lists. RSS is a partial observation: missing books never imply removal. A one-time CSV import remains available from each list for historical coverage. **Find new Goodreads shelves** explicitly refreshes discovery; new shelves are not automatically followed. The profile/RSS key is stored encrypted per reader and never returned in account views. Changing the linked profile leaves previously tracked lists intact, and they remain visible under Tracked lists.

## StoryGraph

Paste `_storygraph_session` and `remember_user_token` from the browser cookie list for `app.thestorygraph.com`. Those cookies are a full StoryGraph login. They are stored encrypted per reader and are never returned in account views. Connecting validates the session, then discovers to-read, currently reading, read, favorites, Up Next when that queue has books, and custom tags. Disconnecting deletes the cookies and leaves followed lists in place; the next check asks the reader to reconnect.

**Add a list** also accepts a StoryGraph shelf or public tag link. Preview and later checks use the saved session, so each reader follows their own copy. One reader's checks run one at a time. A check follows that list's own next page, including the filters on that link, and a blank page does not end it. A redirect away from the page does not replace the saved session. It stops after 20 pages; a preview says when that view stopped early. Connecting, refreshing, and previews wait when StoryGraph has asked for a pause. Refreshing lists after a username change keeps followed shelves and tags pointed at the new name. A sign-in wall or a Cloudflare block holds the check without replacing the saved session or removing books. An account page that names more than one reader is not used to retarget followed lists. Books missing from a later page stay on the local list. Migration `0057_storygraph_accounts` creates the account row.

## Hardcover

Create the API token before pasting it into onboarding or **Settings → Metadata**. [Open Hardcover’s new-key form with these scopes selected](https://hardcover.app/account/api/keys/new?scope=read:catalog+read:me:content+read:lists+read:library:public+read:users+write:lists):

| Scope | Dewarr uses it for |
| --- | --- |
| `read:catalog` | Search, book, author, series, and edition details. Test connection runs a catalog search. |
| `read:me:content` | Your user id, so your lists stay separate from lists you follow. Email and role access are not required. |
| `read:lists` | Your lists, lists you follow, and private lists. |
| `read:library:public` | Public reviews on book pages. |
| `read:users` | Usernames on those reviews. |
| `write:lists` | Adding and removing books on lists you own. |

These six scopes cover discovery, list tracking, reviews, and list write-back. Tokens created before August 2026 already include this access. The same list is in the Metadata setup info button.

Use the saved Hardcover API token, or add it in the reading-accounts step. Browse paginated **My lists** and **Lists I follow**, then select lists to track. The same scheduling, manual refresh, and tracking controls apply. Private lists are supported when the token grants `read:lists`. Existing verified-membership and opt-in writeback controls remain available within each local list and need `write:lists`. Connecting or following a list does not enable writeback or downloads.

## Following authors and series

Choose **Follow author** on a Hardcover author page or **Follow future additions** on a series page. **Following** manages these sources separately from reading-account lists. Catalogs refresh daily, with a manual refresh button. Dewarr reads the complete catalog twice and publishes only matching observations; errors and partial reads preserve the last successful catalog. Catalogs larger than 5,000 books need a narrower source.

After the first verified catalog, preview a Browse, Manual, or Automatic policy. Choose ebook, audiobook, both, or either, plus a download profile. Future books only is the default. In Automatic mode, expand **Include current books** and select at most 25 back-catalog books per reviewed activation. The preview shows owned, missing, and excluded counts. Manual mode uses the existing reviewed list requests. No requests or downloads start merely from pressing Follow.

Compilations, box sets, anthologies, and non-main-series titles are excluded by default. These classifications use Hardcover series flags, integer reading positions, titles, and tags; missing or inaccurate provider metadata can require a per-book exclusion. Optional filters restrict edition language and co-authored books. Follow policies request individual matching books even when a profile normally completes whole series, so the profile cannot bypass follow filters or expand a confirmed back catalog.

New, unreleased books wait on the release calendar until their known release date before source search. Automatic follows require automation permission, approved import routes, and the installation's automatic-download setting. Requests still respect the reader's media permissions and approval rules. Multiple lists and follows retain separate reasons while compatible acquisitions share the existing reservation/download pipeline.

Pause stops refresh and acquisition. Resume and filter changes require a fresh catalog and policy preview. Unfollow withdraws only that follow's reasons; local books and per-book exclusions remain, including if the reader follows the same source again. Author and series sources never write to Hardcover lists.

Discovery notifications integrate with the separate notification service (NOR-30) through `app.notifications.events.record_event`. Initial baselines, failed observations, excluded books, and unchanged refreshes produce no discovery events. Without that service installed, follows and acquisition remain functional and a warning records unavailable notification delivery; discoveries made without the service are not backfilled. Migration `0063_catalog_follows` identifies these subscriptions so the notification service can suppress duplicate generic list events. When integrating independently developed migrations, add a merge revision for the current migration heads before release.

## Checks and persistence

New subscriptions queue their first check immediately and default to hourly checks. The existing durable worker schedules due lists every minute, with up to three minutes of stable per-list jitter. Frequency can be set between 30 minutes and 24 hours. Goodreads uses conditional requests when validators are available, a shared request budget, and delayed retries/backoff. Closing the browser does not stop the worker.

**Check for updates** queues a manual observation. Turning **Track** off pauses checks, fences in-flight observations, and preserves saved books/exclusions. Turn it on again to schedule another check. Discovery, list counts, and last/next-check times are distinct: a feed count is not proof that a complete Goodreads library has been imported.

Deploy the API and UI together and run `uv run alembic upgrade head`. That applies `0046_goodreads_accounts`, `0057_storygraph_accounts`, and the `0058_join_release_heads` revision that backups expect. Keep the worker running for automatic checks.

Tests cover URL validation, bounded same-account profile redirects, tag/empty-shelf discovery, RSS fallback, encrypted keys, per-user isolation, duplicate follows, scheduled checks, pause preservation, private Hardcover lists, StoryGraph shelf and tag follows, and onboarding/settings controls on desktop/mobile. Browser Goodreads and StoryGraph responses are fixtures; no test calls StoryGraph.
