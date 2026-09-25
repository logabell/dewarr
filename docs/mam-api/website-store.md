# MAM website store selections

Source: website text pasted by the user on September 24, 2026. This page records the **website UI**, not the API contract. Compare [Bonus Points Store API](account-and-store.md#bonus-points-store) before implementing actions.

## Upload Credit

500 points per GB of credit, as labeled on the website.

| Points | Website selection |
| ---: | --- |
| 500 | 1 GB |
| 1,250 | 2.5 GB |
| 2,500 | 5 GB |
| 10,000 | 20 GB |
| 25,000 | 50 GB |
| 50,000 | 100 GB |
| Variable | All I can afford |

The API separately specifies a minimum of 50 **GiB** and integer amounts. Smaller website selections cannot be assumed to work through the API.

## VIP Status

Requires Power User or VIP rank plus the upload/ratio requirements of Power User. The website links to [rank requirements](https://www.myanonamouse.net/faq.php#id_22).

Maximum remaining VIP is 90 days (displayed as 12.8 weeks); purchases that would exceed that cap are disallowed. Costs 5,000 points per four weeks, with a four-week minimum or the Max button.

| Points | Website selection |
| ---: | --- |
| 5,000 | 4 weeks |
| 10,000 | 8 weeks |
| 15,000 | 12 weeks |
| Variable | Max me out! |

The pasted account-specific display said VIP expired in 2.74 weeks. That was a transient display value, not a current balance or default. The API only documents `duration="max"`, with a seven-day minimum.

## FreeLeech Wedges

| Cost | Website selection |
| --- | --- |
| 5 cheese | Via cheese |
| 50,000 bonus points | Via points |

The API excerpt lists `spendtype="wedges"` but does not document a cheese/points payment selector.

## Seedtime Fix

Costs 1,000 bonus points to add 72 hours of seeding credit for a selected torrent. Enter its torrent ID. The pasted eligibility text says the torrent must be inactive for seven days, have seven days of leech time, or be five days past completion.

The source links to [finding a torrent ID for seedtime fixes](https://www.myanonamouse.net/guides/?gid=28698). No seedtime-fix API endpoint or parameter was supplied.
