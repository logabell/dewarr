"""Copy-only native review handoff. Absence from Bookdrop is never ownership."""

from datetime import UTC, datetime
from pathlib import PurePosixPath

from app.db.models import Operation
from app.domain import capacity
from app.importing.filesystem import beneath, directory
from app.importing.publication import (
    PublicationError,
    entry_lock,
    object_id,
    private_staging,
    read_receipt,
    specification_fingerprint,
)

REVIEW_STATES = {"awaiting-review", "needs-link", "rejected"}


def recovered_handoff(spec):
    """Do not re-create a consumed file after losing the database acknowledgement.

    A prepared journal with missing staging is uncertain: the atomic move may
    have happened before Grimmory consumed it. Retain it for explicit linking.
    """
    with (
        private_staging(spec.staging_root, spec.journal_root) as staging,
        entry_lock(staging, spec.entry_id),
    ):
        receipt = read_receipt(staging, str(spec.entry_id) + ".json")
        if not receipt:
            return None
        with directory(spec.destination_root) as destination:
            if receipt.get("spec_hash") != specification_fingerprint(spec) or receipt.get(
                "destination_identity"
            ) != object_id(destination):
                raise PublicationError(
                    "Bookdrop receipt or intake mount changed; review the existing handoff"
                )
        if receipt["state"] == "published":
            return receipt
        if receipt["state"] == "prepared":
            try:
                with beneath(staging, receipt["stage_name"], folder=True):
                    pass
            except FileNotFoundError:
                return receipt
    return None


async def record_handoff(db, entry, receipt):
    entry.receipt = receipt
    entry.published_at = entry.published_at or datetime.now(UTC)
    entry.state = "awaiting-review"
    entry.message = (
        "Delivered to the intake folder. Review the EPUB in Grimmory Bookdrop; it "
        "is not yet confirmed in a library."
    )
    entry.next_check_at = entry.run_token = None
    operation = await db.get(Operation, entry.operation_id)
    operation.status, operation.message = "completed", entry.message
    await capacity.release_import(db, entry)


async def observe(adapter, entry):
    spec = entry.specification
    expected = str(
        PurePosixPath(entry.configuration["destination"]["backend_path"])
        / spec["folder"]
        / spec["files"][0]["name"]
    )
    matches = [
        row
        for row in await adapter.bookdrop_files()
        if str(PurePosixPath(row["filePath"])) == expected
        and row["fileSize"] == spec["files"][0]["identity"]["size"]
    ]
    receipt = dict(entry.receipt or {})
    if len(matches) == 1 and (
        not receipt.get("bookdrop_id") or receipt["bookdrop_id"] == matches[0]["id"]
    ):
        receipt["bookdrop_id"] = matches[0]["id"]
        entry.state = "awaiting-review"
        entry.message = (
            "EPUB detected in Grimmory Bookdrop. Review its metadata and choose its "
            "final library there, then link the imported book here."
        )
    else:
        entry.state = "needs-link"
        entry.message = (
            "No matching EPUB is visible in Bookdrop. Check the configured intake "
            "path or whether it was imported or discarded. Link the actual library "
            "copy or mark it rejected; Dewarr will not resend it."
        )
    entry.receipt = receipt


def plan_item(group):
    """Intake names preserve the source and do not depend on library templates."""
    from app.importing.naming import FileMapping, PlannedItem

    media = [file for file in group.files if file.role == "media"]
    valid = (
        group.medium == "ebook"
        and len(media) == 1
        and PurePosixPath(media[0].path).suffix.lower() == ".epub"
    )
    state = (
        "skipped"
        if group.decision == "skip-owned"
        else "ready"
        if valid and group.full_content and group.decision == "import"
        else "held"
    )
    return PlannedItem(
        group_id=group.id,
        work_id=group.work_id,
        version_id=group.version_id,
        medium=group.medium,
        title=group.metadata.title,
        state=state,
        reason=None
        if state == "ready"
        else group.reason or "Bookdrop requires one complete EPUB per book",
        folder="bookdrop/review",
        files=[
            FileMapping(
                source=file.path,
                destination="bookdrop/review/" + PurePosixPath(file.path).name,
                role="media",
            )
            for file in media
        ]
        if valid
        else [],
    )
