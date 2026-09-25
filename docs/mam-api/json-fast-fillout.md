# JSON fast fillout

Source: MAM documentation pasted by the user on September 24, 2026; [source page](https://www.myanonamouse.net/api/object.php/1/JSON+fast+fillout).

Specification for a JSON string/object that fills a new request or torrent upload form. **Omit fields that should not be filled.** No submission endpoint or HTTP method was supplied; this page describes the form object.

| Field | Type | Meaning |
| --- | --- | --- |
| `authors` | list | Strings of author names. |
| `categories` | list | Category numbers for the newer multiple-category system. See the category reference below. |
| `category` | string or int | Category name or ID. |
| `description` | string | Description; for requests, the book description. |
| `flags` | list of strings | Flags to check: `cLang` (Crude Language), `vio` (violence), `sSex` (Some Explicit Sexual Content), `eSex` (Explicit Sexual Content), `abridged` (Abridged content), `lgbt` (LGBTQ+ themed content). |
| `isbn` | string | ISBN. |
| `language` | string or int | Language name or ID. |
| `main_cat` | int | `1` = Fiction; `2` = Non-Fiction. These values are specific to this form specification, not the search API's media-category IDs. |
| `mediaInfo` | string | Media information for a torrent's audio component. |
| `mediaType` | int | Media type ID. See the category reference below. |
| `narrators` | list | Strings of narrator names. |
| `series` | list | Objects containing `name` and `number`. |
| `series[].name` | string | Series name. |
| `series[].number` | string | Included series number(s). The supplied example also uses a numeric value. |
| `subtitle` | string | Book subtitle. |
| `tags` | string | Tags. |
| `thumbnail` | string | Poster image URL. |
| `title` | string | Item title. |

Category and media-type reference linked by the source: [categories.php?new](https://www.myanonamouse.net/tor/json/categories.php?new). Its response was not supplied in this capture.

## Example input

```json
{
  "title": "Torrent title goes here",
  "authors": ["author #1", "author #2"],
  "narrators": ["narrator #1", "narrator #2"],
  "tags": "Tags & Labels go here",
  "description": "Torrent description goes here",
  "series": [
    {"name": "First series name goes here", "number": 3},
    {"name": "Second series name goes here", "number": "1-3"}
  ],
  "subtitle": "Torrent subtitle goes here",
  "thumbnail": "URL for the poster image goes here",
  "language": "French",
  "category": "Ebooks - Art"
}
```
