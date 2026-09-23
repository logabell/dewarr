# Request quotas

Administrators configure limits in **Settings → Request quotas**. A saved user policy
replaces the named role policy; a named role policy replaces the legacy role default;
that default replaces installation limits. Removing a policy restores inheritance.
A saved empty policy explicitly allows unlimited requests.

Each policy can combine daily (24 hours), weekly (7 days), and monthly (30 days)
rolling limits, by ebook, audiobook, or combined media. A Both request consumes one
admission for each missing medium. Either consumes one admission, attributed to the
selected medium. There is also an independent pending-approval cap.

The request form and Activity display remaining counts and bytes. Administrators see
per-user usage on the same settings page. Administrators always bypass limits; the
**Bypass request quotas** permission can grant the same behavior to other users.
Policies can exempt requests explicitly approved by another administrator.

Manual requests exceeding a limit return HTTP 429 with a capacity-return time and
Retry-After header when time alone can release capacity. A pending cap explains that
approval or withdrawal releases its slot. A zero limit, oversized release, or missing
size evidence requires a settings/release change rather than an invented reset time.
Automatic list and series requests retain a **Waiting for quota** target and resume
through the existing reconciliation and list workers. Size holds resume the same way.

Admissions are durable and transactional with request creation. Cancelling, retrying,
or replacing a failed download does not create another book admission for the same
target. A larger replacement reserves the larger size rather than double-counting
both files. Size is checked when an inspected release is selected; its rolling window
starts at that reservation. Existing library copies and joins to committed transfers
are exempt. A quota hold never cancels a submitted transfer.

## Integration contract

Use `acquisition.submit(..., automatic=True)` for additional automatic producers,
with the correct owning user. Existing list policy and series references imply this
behavior. Preserve approval checks and the producer's own authorization. Inspect
`AcquisitionTarget.quota_waiting` and `quota_retry_at` before starting source work;
keep a scheduled retry for holds whose capacity depends on an approval or policy edit.
The `evaluate` function admits eligible held targets when capacity returns.

Replacement workflows must reuse the original intent/target. Do not delete the
`RequestQuotaCharge` ledger or replace its admission timestamp. Source selection uses
`request_quotas.reserve_size`; automatic selection persists size holds after rolling
back its selection savepoint. All work locks, including grouped download membership,
acquire the transaction admission gate first, preventing cross-book batch races and
inverted quota/work locks. Settings use the same gate.

Read-only endpoints: `/api/request-quotas/me`, `/api/request-quotas/users` (admin),
and `/api/request-quotas` (admin). PUT or DELETE `/api/request-quotas/{scope}` saves or
removes a policy; scope is `installation`, `role:<role UUID>`, `role:member`, or
`user:<user UUID>`.
