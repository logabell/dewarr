"""Shared primary edition choices for narrator details and library artwork."""

from sqlalchemy import select

from app.db.models import Work
from app.domain.catalog_titles import title_narrators
from app.domain.visibility import visible_origin_work


async def primary_choices(db, user, mapping, roots):
    choices, dates = {}, {}
    if not roots:
        return choices
    rows = await db.execute(
        select(mapping.c.work_id, Work.metadata_fields["primary_editions"])
        .join(Work, Work.id == mapping.c.origin_id)
        .where(mapping.c.work_id.in_(roots), visible_origin_work(user))
    )
    for root, choices_by_medium in rows:
        for medium, choice in (choices_by_medium or {}).items():
            if medium not in {"ebook", "audio"} or not isinstance(choice, dict):
                continue
            stamp = choice.get("chosen_at", "")
            if stamp >= dates.get((root, medium), ""):
                dates[root, medium] = stamp
                choices.setdefault(root, {})[medium] = choice.get("version_id")
    return choices


def asset_narrators(asset, version_narrators=None):
    return (
        (
            asset.metadata_snapshot.get("narrators")
            or version_narrators
            or title_narrators(asset.title)
        )
        if asset.medium == "audio"
        else []
    )


def edition_order(asset, choice, narrators=None):
    # An available copy is preferable to one last seen in stale inventory.
    return (
        asset.state != "present",
        str(asset.version_id) != choice,
        not bool(narrators),
        asset.created_at,
        str(asset.id),
    )
