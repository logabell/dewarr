# Failed download recovery

Administrators configure recovery under **Settings → Download recovery**. The
defaults are a 24-hour no-progress/zero-seeder window, a five-minute downloader
error grace period, and three total attempts per requested format, including the
first download. MAM disables stall detection by default because quiet torrents
may become available later. Source overrides replace the default policy; an
indexer override such as `prowlarr:12` takes precedence over `prowlarr`.

Only verified client observations advance these timers. Paused, queued and
checking transfers do not count as stalled. An uncertain submission or failed
client connection must reconcile before it can cause a replacement.

A rejected release is blocklisted for its book and medium, including its known
artifact and torrent identities. Recovery tries saved eligible results, then
refreshes the original request's search once if necessary. Original frozen
constraints still apply alongside current permissions, configuration, quotas
and capacity. Exhaustion or the attempt cap pauses the request with an explanation.
Removing a blocklist entry permits selection again, but never releases a recorded
transfer's identity claim or causes it to be submitted a second time.

Expand **Download history and recovery** on a request or a book's Downloads tab
to see its attempts or report wrong/incomplete content. Reports can wait for
administrator approval; pending approvals appear in Download recovery settings.
For a shared pack, each member gets an independent replacement search. Confirmed
copies from that transfer are excluded only for the affected request owners.
Other existing copies can still satisfy their requests.

Cleanup defaults to leaving the transfer in its client. Optional qBittorrent
pause/remove actions reverify identity and configuration and never request file
deletion. Other clients retain their transfers. Leaving a transfer running keeps
its capacity slot occupied; preserved downloaded files keep their storage
reservations. Replacement imports use separate deterministic folders and preserve
the original library files and import receipts. Rejected inspections cannot be
imported again.

## Integration notes

Migration `0063_download_recovery` branches from `0061_part_combines` and is joined
with `0062_request_quotas` by `0064_quotas_recovery`. When combined with NOR-30's
independent `0062_notifications` migration, add an Alembic merge revision for that
head and `0064_quotas_recovery`.

Recovery records audit events transactionally. If NOR-30 is installed,
`app.notifications.events.record_event` also receives deduplicated
`download.stalled`, `download.retried` and `download.gave_up` events in the same
transaction. Without that module, recovery remains functional and logs that
notification delivery is unavailable; external delivery requires NOR-30.
