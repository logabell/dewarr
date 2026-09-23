"""Deterministic initial sidecars; never rewrite media or existing library metadata."""

import json
import re
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

from pydantic import Field, model_validator

from app.importing.naming import NamingMetadata, StrictModel


class ExportMetadata(StrictModel):
    medium: str = Field(pattern=r"^(ebook|audio)$")
    naming: NamingMetadata
    description: str | None = Field(default=None, max_length=60000)
    genres: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def valid_xml_values(self):
        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, list):
                for member in value:
                    yield from strings(member)
            elif isinstance(value, dict):
                for member in value.values():
                    yield from strings(member)

        for value in strings(self.model_dump()):
            if any(
                not (
                    character in "\t\n\r"
                    or 0x20 <= ord(character) <= 0xD7FF
                    or 0xE000 <= ord(character) <= 0xFFFD
                    or 0x10000 <= ord(character) <= 0x10FFFF
                )
                for character in value
            ):
                raise ValueError("Export metadata contains an XML-incompatible character")
        if not self.naming.title.strip():
            raise ValueError("An initial metadata export needs a resolved title")
        return self


def valid_isbn(value):
    if not value:
        return None
    cleaned = re.sub(r"[\s-]", "", value).upper()
    if re.fullmatch(r"\d{9}[\dX]", cleaned):
        digits = [int(character) if character != "X" else 10 for character in cleaned]
        if sum((10 - index) * digit for index, digit in enumerate(digits)) % 11 == 0:
            return cleaned
    if re.fullmatch(r"97[89]\d{10}", cleaned):
        if (
            sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(cleaned))
            % 10
            == 0
        ):
            return cleaned
    return None


def initial_sidecars(metadata: ExportMetadata) -> dict[str, str]:
    facts = metadata.naming
    package = ET.Element("package", {"xmlns": "http://www.idpf.org/2007/opf", "version": "2.0"})
    container = ET.SubElement(
        package,
        "metadata",
        {
            "xmlns:dc": "http://purl.org/dc/elements/1.1/",
            "xmlns:opf": "http://www.idpf.org/2007/opf",
        },
    )

    def add(name, value, **attributes):
        if value is not None and str(value).strip():
            ET.SubElement(container, name, attributes).text = str(value).strip()

    # The part label in the title is how a sync recognizes this item as one part of the book.
    add("dc:title", f"{facts.title} ({facts.part_label})" if facts.part_label else facts.title)
    add("dc:subtitle", facts.subtitle)
    for author in dict.fromkeys(name.strip() for name in facts.authors if name.strip()):
        add("dc:creator", author, **{"opf:role": "aut"})
    if metadata.medium == "audio":
        for narrator in dict.fromkeys(name.strip() for name in facts.narrators if name.strip()):
            add("dc:creator", narrator, **{"opf:role": "nrt"})
    add("dc:language", facts.language)
    year = facts.recording_year if metadata.medium == "audio" else facts.edition_year
    if year:
        add("dc:date", f"{year:04}")
    add("dc:publisher", facts.publisher)
    add("dc:description", metadata.description)
    for genre in dict.fromkeys(value.strip() for value in metadata.genres if value.strip()):
        add("dc:subject", genre)
    if isbn := valid_isbn(facts.isbn):
        add("dc:identifier", isbn, **{"opf:scheme": "ISBN"})
    if facts.asin and re.fullmatch(r"[A-Z0-9]{10}", facts.asin):
        add("dc:identifier", facts.asin, **{"opf:scheme": "ASIN"})
    if facts.series and facts.series.strip():
        ET.SubElement(
            container, "meta", {"name": "calibre:series", "content": facts.series.strip()}
        )
        if facts.sequence and facts.sequence.strip():
            ET.SubElement(
                container,
                "meta",
                {"name": "calibre:series_index", "content": facts.sequence.strip()},
            )
    # Edition labels, abridgment and app IDs have no certified OPF import contract
    # in ABS. Keep them in our version/manifest, never invent an ISBN or ASIN.
    return {"metadata.opf": ET.tostring(package, encoding="utf-8", xml_declaration=True).decode()}


def grimmory_sidecars(facts: dict, medium: str, filenames: list[str]) -> dict[str, str]:
    """Write Grimmory's {Book}.metadata.json beside each published media file."""
    title = facts["title"].strip()
    if facts.get("part_index") and facts.get("part_total"):
        title += f" (Part {facts['part_index']} of {facts['part_total']})"
    metadata = {
        "title": title,
        "authors": [name.strip() for name in facts.get("authors") or [] if name.strip()],
    }
    if facts.get("subtitle"):
        metadata["subtitle"] = facts["subtitle"].strip()
    if facts.get("publisher"):
        metadata["publisher"] = facts["publisher"].strip()
    # Grimmory already reads a full date from the file. A year-only catalog value
    # would replace that date with January 1, so the sidecar leaves the date alone.
    if facts.get("language"):
        metadata["language"] = facts["language"].strip()
    isbn = valid_isbn(facts.get("isbn"))
    if isbn:
        metadata["isbn13" if len(isbn) == 13 else "isbn10"] = isbn
    if facts.get("asin") and re.fullmatch(r"[A-Z0-9]{10}", facts["asin"]):
        metadata["identifiers"] = {"asin": facts["asin"]}
    if facts.get("series") and facts["series"].strip():
        series = {"name": facts["series"].strip()}
        if facts.get("sequence") and str(facts["sequence"]).strip():
            try:
                series["number"] = float(facts["sequence"])
            except ValueError:
                series["number"] = facts["sequence"]
        metadata["series"] = series
    if medium == "audio" and facts.get("narrators"):
        names = [name.strip() for name in facts["narrators"] if name.strip()]
        if names:
            metadata["narrator"] = ", ".join(names)
    if type(facts.get("abridged")) is bool:
        metadata["abridged"] = facts["abridged"]
    document = json.dumps(
        {"version": "1.0", "generatedBy": "grimmory", "metadata": metadata},
        ensure_ascii=False,
        sort_keys=True,
    )
    sidecars = {}
    for name in filenames:
        stem = PurePosixPath(name).stem
        if not stem or stem.startswith("."):
            continue
        sidecars[f"{stem}.metadata.json"] = document
    return sidecars
