# Connection health

Dewarr's background worker checks enabled MAM, Prowlarr, AudiobookBay, Soulseek,
and download client connections every five minutes, even with no browser open.
Disabled and deleted connections are excluded. A recent successful source request
or connection test can satisfy the next scheduled check. MAM proxy checks use a
credential-free public IP probe followed by an authenticated MAM request through
the saved routing policy. These checks do not change the seedbox IP or purchase
account benefits.

Settings and the top navigation poll the saved results every 30 seconds. A warning
lists each failed, untested, or stale connection, its last check time, and a link
to settings for administrators. Successful results expire after ten minutes; a
stopped worker therefore cannot leave an old green status visible indefinitely.
Failures remain visible until a successful test. Manual tests update the same
saved status. No configured connection means no warning.

MAM authentication and proxy reachability are separate. A working proxy can still
have a rejected mam_id after its IP changes. A working direct fallback does not
clear the proxy warning. Checks retain the existing cookie rotation, request
serialization, configuration generation checks, service cooldowns, and recovery
mode safeguards. An interrupted MAM session still requires a current mam_id.

The authenticated `/api/health/connections` endpoint reads saved results and never
performs outbound checks. It exposes no credentials or connection URLs. Non-admins
receive generic downloader labels and no settings links. Liveness and readiness
remain independent of optional external services.

## MouseSearch comparison

Reviewed upstream MouseSearch commit
[`b42b0b9`](https://github.com/sevenlayercookie/MouseSearch/tree/b42b0b98d1b81145f6bc6410407d03ae48fb100e).
In `app.py`, `mam_status` calls `fetch_mam_api_data`, which makes an authenticated
`jsonLoad.php` request. `build_mam_proxy_status_payload` distinguishes a proxy
route, direct fallback, and routing failure. `client_status` checks the client and
retries login after an initial failure. Its frontend loads account and client
status, and its transfer monitor broadcasts client status. Dewarr follows this
separation of account, proxy, and client health with its own persistent scheduled
checks and timestamped UI summary; no upstream code was copied for this change.

## Deployment

Apply migration `0069_connection_health`, deploy the rebuilt frontend, and restart
the API and normal background worker. Existing successes are initially unverified
until checked. The first scheduled check runs within five minutes. The restricted
recovery worker does not run these checks.
