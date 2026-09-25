# MAM API reference

Local reference captured from documentation supplied by the user on September 24, 2026. These files record the supplied pages, not a complete export of every MAM API page or live API certification. No authenticated requests or purchases were made to collect them.

## Index

| Document | Contents |
| --- | --- |
| [Torrent search](torrent-search.txt) | `/tor/js/loadSearchJSONbasic.php`: request formats, fields, filters, sorting, pagination, response fields, and examples from the attached text |
| [Account and store endpoints](account-and-store.md) | Bonus store, torrent download, dynamic seedbox, both supplied IP lookup descriptions, account data, and bonus history |
| [JSON fast fillout](json-fast-fillout.md) | Upload/request form object fields and example |
| [Website store options](website-store.md) | Earlier user-supplied website prices and selections; separate from the API contract |
| [Integration notes](integration-notes.md) | API constraints, website differences, ambiguities, and implications for the proposed MAM control center |

The source page index is [MAM API documentation](https://www.myanonamouse.net/api/list.php). The supplied fast-fillout page is [JSON fast fillout](https://www.myanonamouse.net/api/object.php/1/JSON+fast+fillout). Endpoint links in these documents identify the upstream interfaces; accessing them may require authentication or perform an action.

## Source handling

- `torrent-search.txt` preserves the attached `Pasted text.txt`, except that its user-specific `dl` download token is replaced with `[REDACTED_USER_SPECIFIC_DOWNLOAD_TOKEN]`. The interface description and all other sample fields remain intact. The original attachment remains outside the repository.
- The other endpoint pages are transcribed from the user message into readable Markdown tables. Empty example/output sections are explicitly recorded as not supplied. No unspecified response schema, method, price, or parameter is invented.
- Website store information comes from an earlier user message and must not override the API's documented limits.
- Source inconsistencies and implementation recommendations are called out separately in the integration notes.

See also [Dewarr MAM integration](../MAM-INTEGRATION.md). Read this local reference before changing the MAM adapter, account automation, or control-center UI. Refresh the reference when new upstream documentation is supplied.
