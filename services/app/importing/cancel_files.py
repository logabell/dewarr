"""Stop one unpublished item under the same filesystem lock used by publication."""

import json
import os
from contextlib import ExitStack, nullcontext
from uuid import uuid4

from app.importing.filesystem import beneath, directory
from app.importing.publication import (
    PUBLICATION_MARKER,
    PublicationError,
    entry_lock,
    generated_files,
    has_publication_marker,
    object_id,
    private_staging,
    publication_lock,
    published_names,
    read_receipt,
    remove_publication_marker,
    same_object,
    specification_fingerprint,
    sync_directory,
    write_receipt,
)


def remove_stage(staging, receipt, spec, checkpoint):
    with ExitStack() as opened:
        try:
            folder = opened.enter_context(beneath(staging, receipt["stage_name"], folder=True))
        except FileNotFoundError:
            return  # Already removed by an interrupted cancellation.
        if not same_object(folder, receipt["stage_identity"]):
            raise PublicationError("Cancellation staging identity changed")
        media = {file.name: file for file in spec.files}
        known = published_names(spec) | set(generated_files(spec))
        if receipt.get("publication_marker"):
            known.add(PUBLICATION_MARKER)
        names = os.listdir(folder)
        if set(names) - known:
            raise PublicationError("Unrecognized staged files need review before cancellation")
        identities = {}
        # Validate every existing file before removing any of them.
        for filename in names:
            if filename == PUBLICATION_MARKER:
                if not has_publication_marker(folder, receipt):
                    raise PublicationError("Staged publication marker changed")
                with beneath(folder, filename) as file:
                    identities[filename] = object_id(file)
                continue
            expected = receipt.get("partial_files", {}).get(filename)
            if filename in media and spec.mode == "hardlink":
                expected = media[filename].identity
            with beneath(folder, filename) as file:
                if not expected or not same_object(file, expected):
                    raise PublicationError("Staged file ownership changed; cancellation held")
                identities[filename] = object_id(file)
        for filename in names:
            with beneath(folder, filename) as file:
                if not same_object(file, identities[filename]):
                    raise PublicationError("Staged file changed during cancellation")
                os.unlink(filename, dir_fd=folder)
            checkpoint("cancel-file-removed")
        sync_directory(folder)
        with beneath(staging, receipt["stage_name"], folder=True) as current:
            if not same_object(current, receipt["stage_identity"]):
                raise PublicationError("Staged folder changed during cancellation")
            os.rmdir(receipt["stage_name"], dir_fd=staging)
        sync_directory(staging)
        checkpoint("cancel-stage-removed")


def cancel_renamed(root, staging, name, receipt, spec):
    """Never delete a seeding file that qBittorrent already moved into the library."""
    try:
        with beneath(root, spec.folder, folder=True) as item:
            names = set(os.listdir(item))
            media = {file.name for file in spec.files}
            allowed = media | set(generated_files(spec)) | {".torrent"}
            # An empty folder, or only the parked torrent directory, is not the book.
            incomplete = bool(names - {".torrent"}) and not media <= names
            if names - allowed or incomplete:
                raise PublicationError(
                    "The library folder does not match this seeding rename; "
                    "review it before cancelling"
                )
            if not media <= names:
                receipt["state"] = "cancelled"
                write_receipt(staging, name, receipt)
                return receipt
            if receipt.get("stage_identity") and not same_object(item, receipt["stage_identity"]):
                raise PublicationError("Destination exists and belongs to another item")
            receipt["state"] = "published"
            receipt["stage_identity"] = receipt.get("stage_identity") or object_id(item)
            write_receipt(staging, name, receipt)
            return receipt
    except FileNotFoundError:
        receipt["state"] = "cancelled"
        write_receipt(staging, name, receipt)
        return receipt


def cancel_files(spec, *, guard=nullcontext, checkpoint=lambda _: None):
    name = str(spec.entry_id) + ".json"
    with private_staging(spec.staging_root) as staging, directory(spec.destination_root) as root:
        with (
            entry_lock(staging, spec.entry_id),
            publication_lock(staging, json.dumps(object_id(root), sort_keys=True)),
            guard(),
        ):
            receipt = read_receipt(staging, name)
            if receipt is None:
                try:
                    with beneath(root, spec.folder, folder=True) as item:
                        names = set(os.listdir(item))
                        # A leftover empty book folder is not a published copy.
                        if spec.mode != "rename" or names - {".torrent"}:
                            raise PublicationError(
                                "Destination exists without a publication journal; "
                                "review before cancelling"
                            )
                except FileNotFoundError:
                    pass
                receipt = {
                    "schema_version": 1,
                    "entry_id": str(spec.entry_id),
                    "spec_hash": specification_fingerprint(spec),
                    "destination_identity": object_id(root),
                    "stage_name": "item-" + uuid4().hex,
                    "state": "cancelled",
                }
                # A durable tombstone also fences a delayed pre-cancellation worker.
                write_receipt(staging, name, receipt, create=True)
                return receipt
            if (
                receipt.get("entry_id") != str(spec.entry_id)
                or receipt.get("spec_hash") != specification_fingerprint(spec)
                or receipt.get("destination_identity") != object_id(root)
            ):
                raise PublicationError("Cancellation journal or destination identity changed")
            if receipt["state"] == "cancelled":
                return receipt
            if spec.mode == "rename":
                return cancel_renamed(root, staging, name, receipt, spec)
            try:
                with beneath(root, spec.folder, folder=True) as item:
                    if receipt.get("stage_identity") and (
                        same_object(item, receipt["stage_identity"])
                        or has_publication_marker(item, receipt)
                    ):
                        # Keep published bytes, including later metadata edits. The
                        # ordinary confirmation path still verifies media and ABS.
                        receipt["stage_identity"] = object_id(item)
                        receipt["state"] = "published"
                        write_receipt(staging, name, receipt)
                        remove_publication_marker(item, receipt)
                        return receipt
            except FileNotFoundError:
                pass
            if receipt["state"] == "published":
                raise PublicationError(
                    "Previously published item moved; resolve it before stopping"
                )
            if receipt.get("unconfirmed_stages"):
                raise PublicationError(
                    "Unconfirmed staging folders need review before cancellation"
                )
            if receipt.get("stage_identity"):
                if receipt["state"] != "cancelling":
                    try:
                        with beneath(staging, receipt["stage_name"], folder=True) as folder:
                            if not same_object(folder, receipt["stage_identity"]):
                                raise PublicationError("Cancellation staging identity changed")
                    except FileNotFoundError:
                        raise PublicationError(
                            "Staged item is missing; publication may have completed elsewhere"
                        ) from None
                    # Persist cleanup authority before unlinking. Only this state
                    # makes an absent stage safe on a cancellation retry.
                    receipt["state"] = "cancelling"
                    write_receipt(staging, name, receipt)
                remove_stage(staging, receipt, spec, checkpoint)
            else:
                try:
                    with beneath(staging, receipt["stage_name"], folder=True):
                        raise PublicationError(
                            "Unconfirmed staging folder needs review before cancellation"
                        )
                except FileNotFoundError:
                    pass
            receipt["state"] = "cancelled"
            write_receipt(staging, name, receipt)
            checkpoint("cancel-receipt-written")
            return receipt
