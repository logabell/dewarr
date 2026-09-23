# Notifications

In **Settings → Notifications**, add a personal channel. Administrators can also
add installation channels and choose which events members may configure.
Installation channels carry request, review and connection alerts. Private
list/author/series discoveries stay on personal channels. Approval alerts require
permission to manage requests in addition to the administrator's event policy.

Supported destinations:

- **Discord:** an incoming webhook URL; messages use embeds and public catalog
  cover thumbnails when available. Mentions are disabled.
- **ntfy:** the full topic URL, for example `https://ntfy.sh/my-topic`, and an
  optional bearer token. Self-hosted servers work too.
- **Apprise:** one notification URL per line, up to 20. See the
  [Apprise URL documentation](https://github.com/caronc/apprise/wiki).
- **JSON webhook:** an HTTP(S) POST destination and optional bearer token.

Destinations, URL lists and tokens use Dewarr's existing encryption key and are
never returned by the API. Keep the configuration directory with that key when
backing up. Members' destinations must resolve to public IP addresses;
administrators can configure private self-hosted destinations. Redirects are not
followed by the first-class HTTP adapters.

**Send test** queues a synthetic notification to that channel. It sends no book
or user data. The ordinary worker checks the ledger every minute. Last delivery
status and the latest 50 deliveries appear next to the channel. Pausing, editing
or deleting a channel invalidates queued deliveries; current permission changes
also apply before sending. Test requests are limited to one per 30 seconds.

Discovery digests can run every 5 minutes, 15 minutes, hour or day, or be disabled.
Each digest contains up to 200 events. Discord displays the first ten plus a
count; the webhook contains all events. Request and review alerts do not wait for
a digest. Large bursts can require multiple worker passes.

## Delivery guarantees

Deferred database triggers read final transaction state and append events from committed request decisions, download
attempts, import entries, scoped download fulfillment, held/failed operations,
connection status changes, list additions after a baseline, and new series gaps.
Transient states within a transaction do not notify. Rollback discards the event with its producer. A unique event key and a unique
channel/event delivery prevent repeated jobs from duplicating the same event.
New channels do not replay older events.

Delivery runs separately from acquisition/import work. A failed destination
cannot fail the job that produced the event. The delivery transitions to
`sending` and commits **before** making the external request. A worker interruption
or transport timeout becomes `uncertain`; it is never automatically replayed.
A positive response becomes `sent`, a negative response becomes `failed`, and a
revoked route becomes `cancelled`. Restored pending notifications are cancelled.

There is no universal exactly-once network protocol across these services: a
process can stop after the destination accepted a message but before recording
success. Dewarr favors avoiding duplicates in this ambiguity; an uncertain alert
may not have arrived. Investigate the destination and use a fresh test after
repairing it. Apprise can partially deliver a multi-URL channel before reporting
failure; it is not automatically retried. No automatic retry of uncertain or
failed outbound messages is performed.

## Webhook payload, version 1

```json
{
  "schema_version": 1,
  "delivery_id": "94c9ff24-431d-4016-8816-f220d7f5324a",
  "events": [
    {
      "id": "b3f61c75-12b7-4f51-9265-80ca17d8dd5a",
      "type": "import.available",
      "occurred_at": "2026-09-23T18:00:00+00:00",
      "title": "Available in your library",
      "message": "Your requested book is confirmed available in your library",
      "url": "https://dewarr.example/library"
    }
  ]
}
```

`Idempotency-Key` equals `delivery_id`. Consumers should deduplicate on event
`id`; a digest has its own delivery ID. `cover_url` is optional and restricted to
public catalog image hosts without credentials or query strings. Payloads contain
no user identifiers, private feed URLs, credentials, raw provider responses, or
other users' request details. Compatible optional fields may be added to v1;
incompatible changes require a new `schema_version`.

Event types: `request.pending`, `request.approved`, `request.declined`,
`download.started`, `import.available`, `operation.held`, `operation.failed`,
`connection.problem`, `discovery.list`, `discovery.author`, `discovery.series`,
`discovery.gap`, `download.stalled`, `download.retried`, `download.gave_up`.
Channel tests use `test`. Author/series and recovery producers are extension points
implemented by their respective features; configuring their events does not
create follows or recovery policies.

## Producer interface

```python
from app.notifications.events import record_event

await record_event(
    db,
    key=f"follow:{follow_id}:work:{work_id}",
    event_type="discovery.author",
    owner_id=user.id,
    subject_id=work_id,
    title="New author match",
    message="A followed author has a new book",
    path=f"/books/{work_id}",
    cover_url=None,
)
```

Call inside the producer's existing database transaction, after recording the
successful operation/attempt or verified discovery observation. The function
neither commits nor accesses the network. Use a stable key for the logical event,
not a job execution ID. Retries use the same key. Private events require an owner.
Only application-local deep links are accepted; the sole allowed query parameter
is the inspection identifier for the review screen. Pass deliberately selected
public text, never raw snapshots, provider errors, tokens, cookies or feed URLs.
The helper additionally removes URLs and credential assignments from text.

For recovery use `recovery:{recovery_id}:{event_type}`. Follow producers should emit
only after a successful baseline and verified changes; use `discovery.author` or
`discovery.series`. Keep event recording in the same transaction as the new match.
