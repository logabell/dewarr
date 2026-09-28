# Automatic book matching

## The Anxious Generation incident

The saved title used “How the Great Rewiring of Childhood **Caused** an Epidemic of Mental Illness”. Hardcover used “**is Causing**”. Exact subtitle comparison rejected the identifier-backed catalog match despite the same primary title and author. The first source search produced no accepted download; a later source refresh found usable releases. That refresh and the catalog correction happened together, so the earlier lack of an accepted release cannot be attributed solely to the title mismatch.

After download, two separate import checks required manual intervention:

- The EPUB carried the short title “The Anxious Generation”. Its ISBN and author matched, but the importer required equality with the full catalog title. An existing identifier candidate also prevented falling back to the saved request.
- The audiobook contained numbered MP3 files without embedded book metadata or track totals. The saved release identified the book, but completeness and track ordering could not be established by the old rules.

These were matching and import holds, not evidence that the transfers themselves failed.

## Matching behavior

Catalog matching, release assessment and import matching now share title compatibility rules. Case, accents, punctuation and a leading article are normalized. An observed title may omit a descriptive catalog subtitle. Different primary titles, volume numbers and derivative works remain distinct.

Minor differences between two long descriptive subtitles require a shared edition identifier and agreement from the surrounding identity checks. Both subtitles must contain at least eight words, have at least 80% token overlap and at least 92% character similarity. Changes to numbers, negation or edition qualifiers are rejected. Author, language, edition constraints, explicit rejections and ambiguous identifiers still require their existing checks.

Matching revisions include the new rules so cached failures can be reconsidered. Library matches are reconsidered on the next eligible sync; held imports can use **Check again automatically**.

For a single completed download, the saved request and agreeing release metadata may identify otherwise untagged files. Prowlarr releases may supply an exact author/title pair in their name. Series-only credits still need independent embedded evidence. Conflicting file metadata and requests for a specific edition retain stricter review.

One flat numbered audio sequence (`01.mp3` through `12.mp3`, for example) supplies missing track order and totals when the downloaded manifest is complete. Gaps, duplicate numbers, conflicting tags, incomplete disc sets and partial-release labels still block automatic import. Filename order does not establish book identity on its own. Older inspections can acquire a new proposed order and review revision without rewriting their original evidence or an explicit grouping review.

## Review experience

A linked download keeps the book chosen during discovery and its saved library destination. The download review is not a second catalog-matching workflow. The page shows the existing cover, title, author, download status and the next action.

Missing embedded titles or authors do not invalidate a unique edition identifier belonging to the requested book when the saved release also agrees. Another work, conflicting tags, ambiguous identifiers, metadata disputes and specific-edition requirements still block that fallback. The saved request and release evidence are recorded with the automatic import.

Successful automatic imports require no review. A held download can continue automatically with its existing association. When completeness cannot be established, the reviewer confirms the files contain the complete book and adds it to the library; no book or edition picker is shown. Incorrect or ambiguous downloads link to the existing source search for that same book. Correcting catalog metadata uses the existing book metadata page and its Hardcover search.

Downloaded filenames are optional in a small dialog. Naming templates, hashes, grouping identifiers, inspection dumps and the generic catalog picker are absent from linked-download review. The standalone local-folder import tool remains separate. Import retries preserve their frozen plan and request key, and library confirmation is still required before a request becomes satisfied.
