"""Normalize edition evidence without promoting filenames or tracker text to identity."""

import re

from pydantic import Field

from app.domain.catalog_language import catalog_language
from app.domain.identity import normalized
from app.domain.narrators import embedded_names
from app.importing.metadata import valid_isbn
from app.importing.naming import StrictModel

ISBN_KEYS = ("isbn", "isbn10", "isbn13", "isbn_10", "isbn_13")


def language_key(value):
    return catalog_language(value) or ""


def isbn_key(value):
    if not isinstance(value, str):
        return None
    value = re.sub(r"^(?:urn:)?isbn(?:[-_ ]?(?:10|13))?\s*:\s*", "", value.strip(), flags=re.I)
    valid = valid_isbn(value)
    if valid and len(valid) == 10:
        stem = "978" + valid[:9]
        valid = stem + str(
            -sum(int(digit) * (1 if i % 2 == 0 else 3) for i, digit in enumerate(stem)) % 10
        )
    return valid


def isbn_forms(value):
    result = {value}
    if value.startswith("978"):
        stem = value[3:12]
        check = -sum(int(digit) * (10 - i) for i, digit in enumerate(stem)) % 11
        result.add(stem + ("X" if check == 10 else str(check)))
    return result


def identifier(scheme, value):
    if not isinstance(value, str):
        return None
    scheme = normalized(str(scheme or "")).replace("-", "").replace("_", "")
    if scheme.startswith("isbn") or re.match(r"^(?:urn:)?isbn", value, re.I):
        return ("isbn", key) if (key := isbn_key(value)) else None
    if scheme == "asin" or re.match(r"^(?:urn:)?asin:", value, re.I):
        key = re.sub(r"^(?:urn:)?asin:\s*", "", value.strip(), flags=re.I).upper()
        return ("asin", key) if re.fullmatch(r"[A-Z0-9]{10}", key) else None
    if not scheme and (key := isbn_key(value)):
        return "isbn", key
    return None


def catalog_identifiers(values):
    return {found for key, value in values.items() if (found := identifier(key, value))}


class IdentifierEvidence(StrictModel):
    namespace: str
    value: str


class MatchEvidence(StrictModel):
    titles: list[str] = Field(default_factory=list)
    authors: list[list[str]] = Field(default_factory=list)
    narrators: list[list[str]] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    years: list[int] = Field(default_factory=list)
    abridged: list[bool] = Field(default_factory=list)
    identifiers: list[IdentifierEvidence] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


def group_evidence(snapshot, group):
    observed = {file["path"]: file for file in snapshot["files"]}
    titles, authors, narrators, languages, years, abridgments, identifiers, issues = (
        set() for _ in range(8)
    )
    for selected in group.files:
        if selected.role != "media":
            continue
        file = observed[selected.path]
        if file["state"] != "inspected":
            issues.add("Some files have not passed content inspection")
            continue
        metadata, technical = file.get("metadata") or {}, file.get("technical") or {}
        if group.medium == "ebook":
            title = metadata.get("title")
            names = metadata.get("authors") or []
            language = metadata.get("languages") or []
            assertions = metadata.get("identifier_assertions")
            if assertions is None:
                assertions = [
                    {"scheme": None, "value": value} for value in metadata.get("identifiers", [])
                ]
        else:
            tags = technical.get("tags") or {}
            title = tags.get("album")
            names = [tags.get("album_artist") or tags.get("artist")]
            language = [tags.get("language")]
            narrator = tags.get("narrator") or tags.get("composer")
            if narrator:
                credits = embedded_names(narrator)
                if credits:
                    narrators.add(tuple(credits))
            if (year := str(tags.get("year") or tags.get("date") or "")) and re.fullmatch(
                r"\d{4}(?:-\d{2}(?:-\d{2})?)?", year
            ):
                years.add(int(year[:4]))
            if (abridged := tags.get("abridged")) is not None:
                value = normalized(str(abridged))
                if value in {"true", "1", "yes", "abridged"}:
                    abridgments.add(True)
                elif value in {"false", "0", "no", "unabridged"}:
                    abridgments.add(False)
                else:
                    issues.add("Abridgment metadata needs review")
            assertions = [
                {"scheme": key, "value": tags[key]} for key in (*ISBN_KEYS, "asin") if key in tags
            ]
        if title:
            titles.add(normalized(title))
        # EPUB creators and audio tags may use semicolon lists, including a
        # trailing separator. Preserve every credit; commas remain part of names.
        if names := tuple(
            sorted(
                {
                    credit
                    for name in names
                    if isinstance(name, str)
                    for credit in embedded_names(name)
                }
            )
        ):
            authors.add(names)
        languages.update(
            language_key(value) for value in language if isinstance(value, str) and value.strip()
        )
        for assertion in assertions:
            scheme, value = assertion.get("scheme"), assertion.get("value")
            found = identifier(scheme, value)
            if found and len(identifiers) < 64:
                identifiers.add(found)
            elif found:
                issues.add("Too many identifier assertions; review this collection")
            elif (
                scheme
                and normalized(str(scheme)).replace("-", "").replace("_", "")
                in {*ISBN_KEYS, "asin"}
            ) or (isinstance(value, str) and re.match(r"^(?:urn:)?(?:isbn|asin)", value, re.I)):
                issues.add("An embedded edition identifier is invalid")
    for label, values in (
        ("title", titles),
        ("author", authors),
        ("narrator", narrators),
        ("language", languages),
        ("recording year", years),
        ("abridgment", abridgments),
    ):
        if len(values) > 1:
            issues.add(f"Files disagree about {label}")
    if any(sum(key == kind for key, _ in identifiers) > 1 for kind in ("isbn", "asin")):
        issues.add("Multiple edition identifiers require review")
    return MatchEvidence(
        titles=sorted(titles),
        authors=[list(value) for value in sorted(authors)],
        narrators=[list(value) for value in sorted(narrators)],
        languages=sorted(languages),
        years=sorted(years),
        abridged=sorted(abridgments),
        identifiers=[
            IdentifierEvidence(namespace=key, value=value) for key, value in sorted(identifiers)
        ],
        issues=sorted(issues),
    )
