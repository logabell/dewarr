# MAM account and store endpoints

Source: MAM documentation pasted by the user on September 24, 2026. This is a local transcription with formatting normalized. HTTP methods are not specified in the supplied pages unless stated below.

## Bonus Points Store

URI: `https://www.myanonamouse.net/json/bonusBuy.php`

The bonus points store backend.

| Parameter | Type | Requirements and behavior |
| --- | --- | --- |
| `spendtype` | string | Required. Options: `VIP`, `upload`, `wedges`, `gift`, `sendWedge`. |
| `amount` | int or string | Required with upload. Any integer 50 or higher, or the exact string `"Max Affordable "`, including the trailing space. Errors when buying more than affordable or less than 50 GiB total. |
| `duration` | int or string | Required with VIP. The API only accepts `"max"`. Fills to the 90-day limit; minimum purchase is 7 days (604800 seconds). |
| `giftTo` | int or string | Recipient user ID or username for `gift` and `sendWedge`. |

**`gift` and `sendWedge` may not be automated.** The source permits a single call or batch process only from direct user input via user scripts.

The parameter descriptions use `spendtype=vip` and `spendType` in places, while the required parameter table spells the key `spendtype` and lists `VIP` in uppercase. Preserve this distinction rather than assuming case-insensitive handling.

No example input, output parameters, or example output was supplied. No wedge payment selector, wedge quantity parameter, seedtime-fix operation, or fixed VIP-duration option is documented in this excerpt.

## Download Torrent

URI: `/tor/download.php`

Requires a valid session cookie.

| Parameter | Type | Requirements and behavior |
| --- | --- | --- |
| `tid` | int | Required. Torrent ID to download. |
| `fl` | empty | If present, asks the backend to spend a Freeleech wedge to make the torrent personal freeleech if it is not already. Can spend on VIP torrents; the source warns automation users that refunds are unavailable. |

Example input:

```text
/tor/download.php?tid=1234567890&fl
```

Output: a torrent file. No separate output parameter table was supplied.

The [search reference](torrent-search.txt) additionally documents a user-specific download hash for downloads without a session cookie. These hashes are credential-bearing values.

## Dynamic Seedbox IP

URI: `https://t.myanonamouse.net/json/dynamicSeedbox.php`

Sets the dynamic seedbox IP. **Rate limit: once per hour, rolling window.** No input data required.

Requires an IP- or ASN-locked `mam_id` session with permission to set the dynamic seedbox IP. Create that session in the security section of user preferences; it differs from a normal browser session.

The source describes automated use, for example when an OpenVPN tunnel comes up, and recommends MouseHole via Docker as the simplest method:

- [Compendium of information, scripts, and Docker using this API](https://www.myanonamouse.net/f/t/78248)
- [MouseHole recommendation](https://www.myanonamouse.net/f/t/84712/p/1)

### Output parameters

| Parameter | Type | Meaning |
| --- | --- | --- |
| `AS` | string | Organization associated with the IP address. |
| `asn` | int | Autonomous system number. The source table uses lowercase `asn`; its examples use uppercase `ASN`. |
| `ip` | string | IP address from which the call was made. |
| `msg` | string | Result message; see below. |
| `Success` | boolean | Whether the operation succeeded. |

[ASN background linked by the source](https://en.wikipedia.org/wiki/Autonomous_system_(Internet)).

| HTTP status | Message | Meaning |
| --- | --- | --- |
| 200 | `No Change` | The calling IP is already configured. |
| 200 | `Completed` | Update completed. |
| 429 | `Last Change too recent` | Update refused because the previous change was too recent. |
| 403 | `No Session Cookie` | `mam_id` cookie missing. |
| 403 | `Invalid session` | Invalid session, bad cookie, or outside the locked IP/ASN. |
| 403 | `Invalid session - IP mismatch` | Caller does not match the single locked IP. |
| 403 | `Invalid session - ASN mismatch` | Caller is outside the allowed ASN list. |
| 403 | `Invalid session - Invalid Cookie` | Cookie cannot be decoded; bad or corrupted value. |
| 403 | `Incorrect session type - not allowed this function` | Session lacks permission to update the dynamic seedbox. |
| 403 | `Incorrect session type - non-API session` | Normal web session is not permitted for this operation. |

### Example outputs

HTTP 200:

```json
{"Success":true,"msg":"Completed","ip":"10.2.3.4","ASN":1234,"AS":"Org for 1234"}
```

```json
{"Success":true,"msg":"No change","ip":"10.2.3.4","ASN":1234,"AS":"Org for 1234"}
```

HTTP 429:

```json
{"Success":false,"msg":"Last change too recent","ip":"10.2.3.4","ASN":1234,"AS":"Org for 1234"}
```

HTTP 403 (each line is a separate example):

```jsonl
{"Success":false,"msg":"No Session Cookie","ip":"10.2.3.4","ASN":1234,"AS":"Org for 1234"}
{"Success":false,"msg":"Invalid session - IP mismatch","ip":"10.2.3.4","ASN":1234,"AS":"Org for 1234"}
{"Success":false,"msg":"Invalid session - ASN mismatch","ip":"10.2.3.4","ASN":1234,"AS":"Org for 1234"}
{"Success":false,"msg":"Invalid session - Invalid Cookie","ip":"10.2.3.4","ASN":1234,"AS":"Org for 1234"}
{"Success":false,"msg":"Invalid session - Other","ip":"10.2.3.4","ASN":1234,"AS":"Org for 1234"}
{"Success":false,"msg":"Incorrect session type - not allowed this function","ip":"10.2.3.4","ASN":1234,"AS":"Org for 1234"}
{"Success":false,"msg":"Incorrect session type - non-API session","ip":"10.2.3.4","ASN":1234,"AS":"Org for 1234"}
```

### Cookie persistence examples from the source

Initialize with a placeholder session value and persist cookies:

```sh
curl -c /path/docker/persists/mam.cookies -b 'mam_id=long________session________string' https://t.myanonamouse.net/json/dynamicSeedbox.php
```

Future script, `/path/docker/persists/tun_up.sh`:

```sh
#!/bin/bash
curl -c /path/docker/persists/mam.cookies -b /path/docker/persists/mam.cookies https://t.myanonamouse.net/json/dynamicSeedbox.php
```

These are documentation examples, not commands executed during capture.

## IP and ASN lookup

URI: `/json/jsonIp.php`

Returns the caller's IP, ISP/organization name, and ASN as seen by MAM. **Rate limit: one per minute.** No input required.

The source lists these hosts, useful for detecting routing that spreads requests across IPs:

- `https://www.myanonamouse.net/json/jsonIp.php`
- `https://t.myanonamouse.net/json/jsonIp.php`
- `https://t1.myanonamouse.net/json/jsonIp.php`
- `https://t2.myanonamouse.net/json/jsonIp.php`
- `https://t3.myanonamouse.net/json/jsonIp.php`
- `https://t4.myanonamouse.net/json/jsonIp.php`

| Parameter | Type | Meaning |
| --- | --- | --- |
| `AS` | string | Organization that owns the IP. |
| `ASN` | numeric | ASN, for checking against session restrictions. |
| `ip` | string | IP used to access the endpoint. |
| `time` | numeric | Unix timestamp when the request was processed. |

Example output:

```json
{"ip":"51.254.0.0","ASN":16276,"AS":"OVH SAS","time":1776193859}
```

### Second supplied page: IP information endpoint

The user supplied a second description of the same `/json/jsonIp.php` endpoint. It lists availability on `www.myanonamouse.net` and `t.myanonamouse.net`, no example input, and three output fields: `AS` (string, organization), `ASN` (int, origin ASN), and `ip` (string, caller IP). It does not mention `time` or a rate limit; that omission does not negate the first page's limit.

Its example output:

```json
{"ip":"a.b.c.d","ASN":123,"AS":"Some Provider Here"}
```

## Load User Data

URI: `/jsonLoad.php`

Loads updated user data without a full page load.

| Parameter | Type | Behavior |
| --- | --- | --- |
| `clientStats` | empty | When set and `id` is absent, includes clients, torrent counts, and connectability status. Client statistics have a 30-minute cache. |
| `id` | int | User ID whose data to retrieve. |
| `notif` | empty | When set and `id` is absent, includes notifications normally shown in the top banner. |
| `pretty` | empty | When set and `id` is absent, uses `JSON_PRETTY_PRINT`. |
| `snatch_summary` | empty | When set and `id` is absent, includes the basic torrent breakdown from the snatch summary. |

### Output parameters

| Parameter | Type | Meaning |
| --- | --- | --- |
| `classname` | string | Class name. |
| `downloaded` | string | Formatted downloaded amount. |
| `notifs` | associative array | User notifications. |
| `ratio` | string | Upload/download ratio. |
| `seedbonus` | int | Bonus points/karma/seed bonus, when viewing self. |
| `uid` | string | User ID. |
| `uploaded` | string | Formatted uploaded amount. |
| `username` | string | Username. |

No example input or output was supplied. The excerpt does not document `vip_until`, cheese balance, or wedge balance.

## User Bonus History

URI: `/json/userBonusHistory.php`

History of bonus points and wedges.

| Parameter | Type | Behavior |
| --- | --- | --- |
| `other_userid` | int | Other party's user ID for point or wedge gifts. |
| `type` | list | Elements to show: `giftPoints`, `giftWedge`, `wedgePF`, `wedgeGFL`, `torrentThanks`, `millionaires`. |

Example input:

```text
type[]=giftWedge&type[]=wedgePF&type[]=wedgeGFL
```

No output parameter table was supplied. Example output:

```json
[
  {"timestamp":1644804607.0994999,"amount":-1,"type":"wedgePF","tid":330896,"title":1984,"other_userid":null,"other_name":null},
  {"timestamp":1641914230.1078,"amount":-1,"type":"giftWedge","tid":null,"title":null,"other_userid":192566,"other_name":"pezzap7"},
  {"timestamp":1610486584.1027,"amount":-10,"type":"wedgeGFL","tid":220870,"title":"Asimov's Robot, Empire, and Foundation Series","other_userid":null,"other_name":null}
]
```

This example is a top-level array, unlike endpoints returning a JSON object. The example's first `title` is numeric; do not silently treat the example as a guarantee that every title is a string.
