"""Read publication evidence without opening any file for writing or creating locks."""

import asyncio
import os
import time
from pathlib import Path

from app.domain.recovery_scans import ScanHeld, digest
from app.importing.filesystem import beneath, directory, identity, source_scope
from app.importing.publication import (
    PublicationSpec,
    checked_source,
    conversion_inputs,
    has_publication_marker,
    journal_fd,
    object_id,
    private_staging,
    published_names,
    read_receipt,
    same_object,
    specification_fingerprint,
    verify_item,
)
from app.importing.storage import configured_storage_locations
from app.state_bundle import journal_name

MAX_JOURNAL_BYTES = 256 * 1024 * 1024


def _published_media_left(destination, spec) -> bool:
    """True when a published book file is no longer in its folder."""
    try:
        with beneath(destination, spec.folder, folder=True) as folder:
            for file in spec.files:
                try:
                    with beneath(folder, file.name):
                        pass
                except FileNotFoundError:
                    return True
            return False
    except FileNotFoundError:
        return True


def read_publication(entry, roots):
    """Return verified evidence and its original receipt without modifying either."""
    if not entry["specification"]:
        return (
            "unobserved",
            "No frozen publication specification exists",
            {"saved_state": entry["state"]},
            None,
        )
    spec = PublicationSpec.model_validate(entry["specification"])
    if (
        spec.entry_id != entry["id"]
        or str(spec.source_root) not in roots["import_sources"].values()
        or str(spec.destination_root) not in roots["import_destinations"].values()
        or (spec.staging_root, spec.journal_root) not in configured_storage_locations(roots)
    ):
        raise ScanHeld("Saved publication roots do not match current mounted roots")
    deadline = time.monotonic() + 60
    evidence = {
        "saved_state": entry["state"],
        "specification_digest": specification_fingerprint(spec),
        "folder": spec.folder,
    }
    with (
        private_staging(spec.staging_root, spec.journal_root) as staging,
        directory(spec.destination_root) as destination,
    ):
        receipt = read_receipt(staging, str(entry["id"]) + ".json")
        evidence["journal_present"] = receipt is not None
        if receipt is not None:
            if (
                not isinstance(receipt, dict)
                or receipt.get("entry_id") != str(entry["id"])
                or receipt.get("spec_hash") != specification_fingerprint(spec)
                or receipt.get("destination_identity") != object_id(destination)
            ):
                raise ScanHeld("Publication journal identity does not match the frozen import")
            evidence["journal_digest"] = digest(receipt)
            evidence["journal_state"] = receipt.get("state")
        try:
            with beneath(destination, spec.folder, folder=True) as folder:
                before = identity(os.fstat(folder))
                if (
                    not receipt
                    or not receipt.get("stage_identity")
                    or (
                        not same_object(folder, receipt["stage_identity"])
                        and not has_publication_marker(folder, receipt)
                    )
                ):
                    return (
                        "conflict",
                        "Destination exists without matching publication ownership evidence",
                        evidence,
                        receipt,
                    )
                files = publication_identities(folder, spec)
                verify_item(folder, spec, deadline, receipt.get("derived"), receipt)
                if files != publication_identities(folder, spec):
                    raise ScanHeld("Published media or metadata changed during observation")
                evidence["media_identities"] = {name: files[name] for name in published_names(spec)}
                if before != identity(os.fstat(folder)):
                    raise ScanHeld("Published directory changed during observation")
                evidence["destination_identity"] = object_id(folder)
                state, message = (
                    "published",
                    "Published files match the frozen manifest; backend confirmation is separate",
                )
        except FileNotFoundError:
            state, message = "missing", "The saved publication is not visible at its destination"
            if receipt and receipt.get("stage_name"):
                # beneath rejects traversal and symlinks; require a single generated stage leaf.
                name = receipt["stage_name"]
                if not isinstance(name, str) or not name.startswith("item-") or "/" in name:
                    raise ScanHeld("Invalid publication stage reference") from None
                try:
                    with beneath(staging, name, folder=True) as stage:
                        if not receipt.get("stage_identity") or not same_object(
                            stage, receipt["stage_identity"]
                        ):
                            raise ScanHeld("Staged directory identity changed")
                        evidence["stage_identity"] = object_id(stage)
                        state, message = (
                            "staged",
                            "Staged files need reconciliation; this scan publishes nothing",
                        )
                except FileNotFoundError:
                    pass
            # Grimmory can rename a published book onto its library pattern and
            # remove the original folder. The journal still proves publication.
            if (
                state == "missing"
                and receipt
                and receipt.get("state") == "published"
                and _published_media_left(destination, spec)
            ):
                evidence["relocated"] = True
                state, message = (
                    "relocated",
                    "Published files left their folder. Grimmory can still confirm the book.",
                )
        try:
            with (
                directory(spec.source_root) as source_root,
                source_scope(source_root, spec.source_relative, spec.source_kind) as source,
            ):
                for file in (*spec.files, *conversion_inputs(spec)):
                    checked_source(source, file, deadline)
            evidence["source"] = "matches-frozen-files"
        except (OSError, ValueError):
            evidence["source"] = "unavailable-or-changed"
            if state not in {"published", "relocated"}:
                state, message = (
                    "changed",
                    "The source or staged publication needs reconciliation before any file action",
                )
        if receipt != read_receipt(staging, str(entry["id"]) + ".json"):
            raise ScanHeld("Publication journal changed during observation")
        with (
            directory(spec.destination_root) as current_destination,
            private_staging(spec.staging_root, spec.journal_root) as current_staging,
        ):
            if (
                object_id(current_destination) != object_id(destination)
                or object_id(current_staging) != object_id(staging)
                or object_id(journal_fd(current_staging)) != object_id(journal_fd(staging))
            ):
                raise ScanHeld("Publication roots changed during observation")
            if state == "published":
                # An unchanged open child can outlive a renamed/replaced ancestor.
                # Rewalk its current name, not just the root and held descriptors.
                with beneath(current_destination, spec.folder, folder=True) as current_folder:
                    if (
                        identity(os.fstat(current_folder)) != before
                        or publication_identities(current_folder, spec) != files
                    ):
                        raise ScanHeld("Published path changed during observation")
        return state, message, evidence, receipt


def publication_identities(folder, spec):
    result = {}
    for name in published_names(spec) | set(spec.sidecars) | set(spec.binary_sidecars):
        with beneath(folder, name) as media:
            result[name] = identity(os.fstat(media))
    return result


def observe_entry(entry, roots):
    return read_publication(entry, roots)[:3]


def grimmory_publication_ids(inputs) -> set[str]:
    """Import entries whose destination library is Grimmory."""
    destinations = {str(row["id"]): row for row in inputs.get("import_destinations", [])}
    libraries = {str(row["id"]): row for row in inputs.get("libraries", [])}
    integrations = {str(row["id"]): row for row in inputs.get("integrations", [])}
    selected = set()
    for entry in inputs.get("import_entries", []):
        destination = destinations.get(str(entry.get("destination_id")))
        library = libraries.get(str(destination["library_id"])) if destination else None
        integration = integrations.get(str(library["integration_id"])) if library else None
        if integration and integration.get("kind") == "grimmory":
            selected.add(str(entry["id"]))
    return selected


def publication_state(entry, roots, *, grimmory: bool):
    """Filesystem observation. Only Grimmory keeps a moved book reviewable."""
    state, message, evidence = observe_entry(entry, roots)
    if state == "relocated" and not grimmory:
        evidence = {key: value for key, value in evidence.items() if key != "relocated"}
        return "missing", "The saved publication is not visible at its destination", evidence
    return state, message, evidence


def journal_census(path, journal_root=None):
    records = []
    deadline = time.monotonic() + 60
    total_bytes = count = 0
    with private_staging(Path(path), journal_root) as root:
        control = journal_fd(root)
        before = {fd: identity(os.fstat(fd)) for fd in {int(root), control}}
        for fd in before:
            with os.scandir(fd) as entries:
                for entry in entries:
                    if time.monotonic() > deadline:
                        raise ScanHeld("Journal census exceeded its read deadline")
                    count += 1
                    if count > 30000:
                        raise ScanHeld("Staging census exceeds 30,000 entries")
                    if fd == control and entry.name.endswith(".json"):
                        if not journal_name(entry.name):
                            raise ScanHeld("Unrecognized journal filename in the journal root")
                        size = entry.stat(follow_symlinks=False).st_size
                        total_bytes += size
                        if total_bytes > MAX_JOURNAL_BYTES:
                            raise ScanHeld("Journal census exceeds 256 MiB")
                        receipt = read_receipt(control, entry.name)
                        if receipt is None and not size:
                            continue
                        if not isinstance(receipt, dict):
                            raise ScanHeld("Invalid publication journal")
                        if journal_root and receipt.get("staging_root") != str(path):
                            # One protected directory serves multiple independent media mounts.
                            if not receipt.get("staging_root"):
                                raise ScanHeld("Journal has no media staging root")
                            continue
                        records.append(
                            {
                                "name": entry.name,
                                "digest": digest(receipt),
                                "entry_id": receipt.get("entry_id"),
                                "state": receipt.get("state"),
                                "stage_name": receipt.get("stage_name"),
                            }
                        )
                    elif fd == root and entry.name.startswith("item-"):
                        records.append({"name": entry.name, "kind": "stage"})
        if any(value != identity(os.fstat(fd)) for fd, value in before.items()):
            raise ScanHeld("Staging or journal root changed during census")
        with private_staging(Path(path), journal_root) as current:
            if object_id(current) != object_id(root) or object_id(journal_fd(current)) != object_id(
                control
            ):
                raise ScanHeld("Staging or journal path changed during census")
    return records


async def observe_files(inputs, writer):
    roots = inputs["roots"]
    grimmory_ids = grimmory_publication_ids(inputs)
    for entry in inputs["import_entries"]:
        await writer.pulse()
        try:
            state, message, evidence = await asyncio.to_thread(
                publication_state,
                entry,
                roots,
                grimmory=str(entry["id"]) in grimmory_ids,
            )
        except Exception:
            state, message, evidence = (
                "blocked",
                "Publication evidence could not be verified; files were left unchanged",
                {"saved_state": entry["state"]},
            )
        await writer.add(
            "files",
            state,
            (entry["specification"] or {}).get("folder") or "Import " + str(entry["id"])[:12],
            message,
            entity_id=entry["id"],
            evidence=evidence,
        )
    locations = configured_storage_locations(roots)
    for stage, journals in sorted(locations, key=lambda pair: (str(pair[0]), str(pair[1]))):
        await writer.pulse()
        try:
            records = await asyncio.to_thread(journal_census, stage, journals)
        except Exception:
            await writer.add(
                "files",
                "blocked",
                "Publication journals",
                "The current journal root could not be completely observed",
            )
            continue
        known = {str(entry["id"]) for entry in inputs["import_entries"]}
        stages = {r.get("stage_name") for r in records if r.get("entry_id")}
        for record in records:
            if record.get("kind") == "stage" and record["name"] in stages:
                continue
            if (
                record.get("kind") != "stage"
                and record["name"][:-5] in known
                and record.get("entry_id") == record["name"][:-5]
            ):
                continue
            await writer.add(
                "files",
                "untracked",
                "Untracked publication evidence",
                "Current staging evidence is not accounted for by the restored import ledger",
                evidence=record,
            )
        await writer.add(
            "files",
            "observed",
            "Publication journals",
            "Current journal and stage names were inspected without changing them",
            evidence={"entries": len(records)},
        )
    if not locations and inputs["import_entries"]:
        await writer.add(
            "files",
            "blocked",
            "Publication journals",
            "Configure the preserved staging root before reconciling import history",
        )
