import asyncio
import hashlib
import hmac
import json as json_module
import math
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

from sqlalchemy import delete

from app.adapters.contracts import AdapterError, FailureKind, MutationError
from app.adapters.http import JsonEndpoint
from app.config import get_settings
from app.db.models import ProviderBudget, ProviderCache
from app.db.session import session_factory
from app.domain.cache_entries import read_through
from app.domain.catalog_cache_policy import lifetime
from app.domain.operations import transaction_lock

REQUEST_INTERVAL = 1.1


def retry_delay(headers, now):
    delays = []
    retry = headers.get("retry-after", "")
    if retry.isdigit():
        delays.append(int(retry))
    elif retry:
        try:
            delays.append(max(0, (parsedate_to_datetime(retry) - now).total_seconds()))
        except (TypeError, ValueError):
            pass
    for bucket in headers.get("ratelimit", "").split(","):
        remaining = re.search(r"(?:^|;)\s*r=(\d+)", bucket)
        reset = re.search(r"(?:^|;)\s*t=(\d+)", bucket)
        if remaining and reset and int(remaining[1]) == 0:
            delays.append(int(reset[1]))
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            delays.append(max(0, float(headers["x-ratelimit-reset"]) - now.timestamp()))
        except (KeyError, ValueError):
            pass
    return min(max(delays, default=0), 7 * 86400)


class CatalogGateway:
    """Persisted HTTP cache and credential-wide budget; never hold a transaction over I/O."""

    def __init__(
        self,
        provider,
        scope,
        token=None,
        *,
        force=False,
        transport=None,
        cache=True,
        on_stale=None,
        request_interval=REQUEST_INTERVAL,
    ):
        settings = get_settings()
        endpoint = settings.hardcover_url if provider == "hardcover" else settings.openlibrary_url
        self.http = JsonEndpoint(endpoint, token, transport=transport)
        self.provider, self.scope, self.force = provider, scope, force
        digest = hmac.new(
            settings.encryption_key(), (token or provider).encode(), hashlib.sha256
        ).hexdigest()
        self.budget_key = f"{provider}:{digest}"
        self.stale, self.warning, self.used_keys = False, None, []
        self.observed_values = {}
        self.endpoint = endpoint
        self.cache = cache
        self.on_stale = on_stale
        self.request_interval = max(REQUEST_INTERVAL, request_interval)

    async def __aenter__(self):
        await self.http.__aenter__()
        return self

    async def __aexit__(self, *args):
        await self.http.__aexit__(*args)

    async def reserve(self):
        async with session_factory()() as db, db.begin():
            await transaction_lock(db, self.budget_key)
            budget = await db.get(ProviderBudget, self.budget_key)
            now = datetime.now(UTC)
            if not budget:
                budget = ProviderBudget(key=self.budget_key, next_request_at=now)
                db.add(budget)
            due = max(now, budget.next_request_at, budget.blocked_until or now)
            wait = (due - now).total_seconds()
            if wait > 5:
                raise AdapterError(
                    FailureKind.RATE_LIMIT,
                    "This provider is cooling down. Try again later.",
                    retry_after=math.ceil(wait),
                )
            budget.next_request_at = due + timedelta(seconds=self.request_interval)
        if wait > 0:
            await asyncio.sleep(wait)

    async def cooldown(self, delay):
        if not delay:
            return
        async with session_factory()() as db, db.begin():
            await transaction_lock(db, self.budget_key)
            record = await db.get(ProviderBudget, self.budget_key)
            deadline = datetime.now(UTC) + timedelta(seconds=delay)
            if record:
                record.blocked_until = max(record.blocked_until or deadline, deadline)

    async def request(self, method, path, *, params=None, json=None):
        material = [self.provider, self.endpoint, self.scope, method, path, params, json]
        key = hashlib.sha256(json_module.dumps(material, sort_keys=True).encode()).hexdigest()
        self.used_keys.append(key)

        async def load():
            await self.reserve()
            try:
                response = await self.http.request(method, path, params=params, json=json)
            except AdapterError as error:
                delay = retry_delay(self.http.response_headers, datetime.now(UTC))
                if error.kind == FailureKind.RATE_LIMIT:
                    delay = max(delay, 60)
                await self.cooldown(delay)
                if delay:
                    error.retry_after = max(error.retry_after or 0, math.ceil(delay))
                raise
            await self.cooldown(retry_delay(self.http.response_headers, datetime.now(UTC)))
            return response

        if not self.cache:
            return await load()

        def cacheable(response):
            data = response.get("data")
            search = data.get("search") if isinstance(data, dict) else None
            return not (
                response.get("errors")
                or response.get("error")
                or (isinstance(search, dict) and search.get("error"))
                or ("data" in response and not isinstance(data, dict))
            )

        value, stale = await read_through(
            key,
            load,
            fresh_for=lifetime(self.provider, path, json),
            force=self.force,
            on_stale=self.on_stale,
            cacheable=cacheable,
        )
        # Adapters may reject or mutate a response after another request refreshes it.
        # Retain the returned payload so invalidation cannot erase different, newer data.
        self.observed_values[key] = deepcopy(value)
        if stale:
            self.stale = True
            self.warning = (
                "Showing cached catalog data while a background refresh is pending."
                if self.on_stale
                else "Provider unavailable; showing previously cached catalog data."
            )
        return value

    async def invalidate(self):
        async with session_factory()() as db, db.begin():
            for key, value in sorted(self.observed_values.items()):
                await db.execute(
                    delete(ProviderCache).where(
                        ProviderCache.key == key, ProviderCache.value == value
                    )
                )

    async def mutate(self, document, variables):
        """One uncached mutation attempt; callers persist intent before invoking it."""
        if self.provider != "hardcover":
            raise MutationError(
                FailureKind.UNSUPPORTED,
                "This catalog provider does not support list changes",
                may_have_applied=False,
            )
        try:
            await self.reserve()
        except AdapterError as error:
            raise MutationError(
                error.kind,
                str(error),
                may_have_applied=False,
                retry_after=error.retry_after,
            ) from None
        try:
            response = await self.http.request(
                "POST", "v1/graphql", json={"query": document, "variables": variables}
            )
        except AdapterError as error:
            delay = retry_delay(self.http.response_headers, datetime.now(UTC))
            if error.kind == FailureKind.RATE_LIMIT:
                delay = max(delay, 60)
            await self.cooldown(delay)
            raise MutationError(
                error.kind,
                str(error),
                may_have_applied=error.kind
                not in {
                    FailureKind.AUTHENTICATION,
                    FailureKind.PERMISSION,
                    FailureKind.RATE_LIMIT,
                },
                retry_after=max(error.retry_after or 0, math.ceil(delay)) or None,
            ) from None
        await self.cooldown(retry_delay(self.http.response_headers, datetime.now(UTC)))
        return response
