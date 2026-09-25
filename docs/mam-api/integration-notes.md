# Implications for Dewarr's MAM control center

Engineering notes based on the user-supplied September 24, 2026 documentation. These notes preserve the API review points. The current implementation is documented in [MAM account control center](../MAM-CONTROL-CENTER.md); live purchases have not been tested.

## Purchase selections must follow the API contract

- The 50 GiB minimum in the existing upload-purchase model agrees with the supplied API documentation. It is not evidence of an incorrect API restriction. The UI should explain it and offer supported choices, such as 50 GiB, 100 GiB, and maximum affordable.
- The exact maximum-affordable wire value is `"Max Affordable "`, with one trailing ASCII space. Preserve it in serialization and test it explicitly; do not trim it or substitute `"max"`.
- Fixed upload amounts can be any integer at least 50; website options for 1, 2.5, 5, and 20 GB are not supported by this documented API.
- A ratio threshold is separate from a purchase amount. Keep the threshold independently editable; it does not inherit the 50 GiB purchase minimum. A dropdown for purchases should not impose a ratio of 50.
- VIP only supports `duration="max"` through this API, with a seven-day minimum and a 90-day remaining-time cap. The website's 4/8/12-week buttons are not API duration options.
- `wedges` is a documented spend type, but the excerpt omits its payment selector and response schema. Do not invent support for paying with cheese. Seedtime fixes also need additional API evidence before implementing a direct action.
- `gift` and `sendWedge` are explicitly prohibited from automation. They are outside the proposed recurring account rules.

## Account data

`/jsonLoad.php` documents the fields needed for ratio, uploaded amount, downloaded amount, bonus points, account class, user ID, and username. For self data, omit `id`; `seedbonus` is specifically documented for self views.

Uploaded/downloaded values and ratio are strings. Preserve formatted display values and treat missing/unparseable values as unknown, not zero. Do not assume a raw byte integer. Additional fields currently used by the application, including `vip_until`, need separate evidence: they are absent from this excerpt. Cheese and wedge balances are also absent.

The 30-minute cache statement applies to `clientStats`, not necessarily to every account field. There is no supplied general account-data polling rate. Choose conservative refresh behavior without claiming it is an upstream quota.

## Scheduling and download behavior

- Dynamic seedbox updates have a rolling one-hour rate limit. Review actual update attempts independently from IP-poll intervals; changes in IP/ASN must not cause repeated updates inside that window. Persist relevant cooldown state across workers and restarts.
- `/json/jsonIp.php` has a one-per-minute limit. Both supplied descriptions refer to the same endpoint; the shorter description does not remove that limit.
- A presence-only `fl` download parameter can spend a wedge even on a VIP torrent. Gate it behind explicit user policy and known torrent/account state. The search source also notes a 5–20 minute delay for `personal_freeleech` and `my_snatched` observations.
- Bonus history returns a top-level array in its example. An HTTP adapter that only accepts JSON objects needs explicit handling before exposing this endpoint.

## Source ambiguities to retain

- The store table lists `VIP`, while a description says `spendtype=vip`; use the documented enum spelling until confirmed otherwise.
- Seedbox output tables say `asn`, but examples say `ASN`. Message capitalization also varies (`No Change` versus `No change`).
- Search `titleAsc` is described as descending and `titleDesc` as ascending. Preserve the source contradiction; do not silently assert the intended order.
- Search response prose says `title` and `filetypes`; its sample uses `name` and `filetype`. Some fields typed as integers or booleans appear as numeric strings in the example.
- The search sample includes `browseStart`, `bannerLink`, `bookmarks`, `browseFlagsHideVsShow`, and `thumbnail` beyond the documented table. `searchIn` explicitly says its enum list is still to come. Do not infer complete contracts from these examples.
- Search examples are illustrative: the response includes an ellipsis and is not a complete JSON fixture. `series_info` is described as ID/name pairs but the example maps IDs to name/position arrays.
- Search `total` is the number loaded and can expand during pagination; `total_found` is the total found. They are not interchangeable.
- Fast-fillout `main_cat` uses Fiction/Non-Fiction IDs 1/2; search `main_cat` uses media IDs 13/14/15/16.

## Evidence still needed

The supplied API pages have no store success/failure response examples, no documented seedtime-fix API, no wedge payment details, and no cheese/wedge balance fields. The linked category metadata payload and complete `searchIn` values were not supplied. This reference is enough to correct the planned control-center scope, but does not prove every website action is available through the API.
