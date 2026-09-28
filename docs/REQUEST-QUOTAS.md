# Unlimited requests

Request quotas have been removed. There are no daily, weekly, monthly, byte or
pending-request caps, regardless of account, role or previously saved policy.
Request permissions and approval requirements still apply.

Existing quota records are retained as historical data and are never enforced.
Paused quota targets become eligible when their requests are evaluated again.
The legacy usage endpoints report unlimited usage; attempts to create new caps
through the retired policy endpoint return 410. Quota settings, usage summaries
and the quota-bypass permission control have been removed from the UI.

Download dispatch has no daily or concurrent-transfer cap. Transfer sizes and
replacement attempts are not capped. Automatic selection can inspect every
candidate in its search results. Actual disk-space accounting, file validation,
worker resource bounds and upstream provider rate limits continue to apply.
