"""Scheduled MyAnonamouse account actions. Nothing here runs unless an administrator enabled it."""

import logging
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from app.adapters.contracts import AdapterError
from app.adapters.mam import (
    SEEDBOX_REFRESH,
    HelperCommand,
    HelperResult,
    stored_automation,
)
from app.db.models import SourceConnection, User
from app.db.session import session_factory

logger = logging.getLogger(__name__)


def _age(state, key, now):
    raw = state.get(key) if isinstance(state, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        then = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return now - then.astimezone(UTC)


def due_command(raw_settings, state, now):
    settings = stored_automation(raw_settings)
    state = state if isinstance(state, dict) else {}
    command = HelperCommand(
        ratio_below=settings.ratio_below,
        ratio_buy_gb=settings.ratio_buy_gb,
        buffer_below_gb=settings.buffer_below_gb,
        buffer_buy_gb=settings.buffer_buy_gb,
        bonus_above=settings.bonus_above,
        bonus_buy_gb=settings.bonus_buy_gb,
    )
    if settings.seedbox_ip:
        age = _age(state, "seedbox_at", now)
        if age is None or age >= timedelta(seconds=max(3600, settings.seedbox_interval_seconds)):
            command.seedbox = True
            ip = state.get("seedbox_ip")
            asn = state.get("seedbox_asn")
            command.known_ip = ip if isinstance(ip, str) else None
            command.known_asn = asn if isinstance(asn, str) else None
            authorized = _age(state, "seedbox_authorized_at", now)
            command.seedbox_stale = authorized is None or authorized >= SEEDBOX_REFRESH
    if settings.auto_vip:
        age = _age(state, "vip_at", now)
        if age is None or age >= timedelta(hours=settings.vip_interval_hours):
            command.vip = True
    if settings.protect_ratio or settings.maintain_buffer or settings.spend_bonus:
        age = _age(state, "upload_at", now)
        if age is None or age >= timedelta(hours=settings.upload_interval_hours):
            command.upload_ratio = settings.protect_ratio
            command.upload_buffer = settings.maintain_buffer
            command.upload_bonus = settings.spend_bonus
    if not (command.seedbox or command.vip or command.uploads):
        return None
    return command


def next_automation_state(state, command, result, now):
    state = dict(state or {})
    stamp = now.astimezone(UTC).isoformat()
    if not isinstance(command, HelperCommand):
        command = HelperCommand.model_validate(command or {})
    if not isinstance(result, HelperResult):
        if command.seedbox:
            state["seedbox_at"] = stamp
        if command.vip:
            state["vip_at"] = stamp
        if command.uploads:
            state["upload_at"] = stamp
        return state
    if "seedbox" in result.checked:
        state["seedbox_at"] = stamp
        if result.seedbox_ip and (result.seedbox_authorized or result.seedbox_unchanged):
            state["seedbox_ip"] = result.seedbox_ip
            state["seedbox_asn"] = result.seedbox_asn
        if result.seedbox_authorized:
            state["seedbox_authorized_at"] = stamp
    if "vip" in result.checked:
        state["vip_at"] = stamp
    if "upload" in result.checked:
        state["upload_at"] = stamp
    return state


def _upload_command(command):
    return HelperCommand(
        upload_ratio=command.upload_ratio,
        upload_buffer=command.upload_buffer,
        upload_bonus=command.upload_bonus,
        ratio_below=command.ratio_below,
        ratio_buy_gb=command.ratio_buy_gb,
        buffer_below_gb=command.buffer_below_gb,
        buffer_buy_gb=command.buffer_buy_gb,
        bonus_above=command.bonus_above,
        bonus_buy_gb=command.bonus_buy_gb,
    )


async def _once(admin_id, command):
    from app.domain.source_network import source_call

    try:
        await source_call(admin_id, "maintain", command)
    except AdapterError as error:
        logger.warning("Account automation did not finish (%s)", error.kind.value)
    except HTTPException:
        return


async def run():
    from app.config import get_settings

    if get_settings().recovery_mode:
        return
    async with session_factory()() as db:
        row = await db.get(SourceConnection, "mam")
        if not row or not row.enabled:
            return
        command = due_command(row.automation, row.automation_state, datetime.now(UTC))
        if command is None:
            return
        admin_id = await db.scalar(
            select(User.id).where(User.role == "admin", User.active.is_(True)).limit(1)
        )
    if admin_id is None:
        return
    if command.seedbox:
        await _once(
            admin_id,
            HelperCommand(
                seedbox=True,
                known_ip=command.known_ip,
                known_asn=command.known_asn,
                seedbox_stale=command.seedbox_stale,
            ),
        )
    if command.vip:
        await _once(admin_id, HelperCommand(vip=True))
    if command.uploads:
        await _once(admin_id, _upload_command(command))
