"""Disposable browser worker: only Goodreads HTTP is routed through the local feed fixture."""

import asyncio
import logging
from urllib.parse import urlsplit

import httpx

from app.adapters.goodreads import fetch_feed
from app.config import get_settings
from app.domain import list_subscriptions, recovery_observers
from app.jobs import worker

if not urlsplit(get_settings().database_url.get_secret_value()).path.endswith("_browser_test"):
    raise SystemExit("Fixture worker requires the disposable browser database")


async def resolver(host):
    assert host == "www.goodreads.com"
    return ["1.1.1.1"]


async def handler(request):
    assert request.url.host == "1.1.1.1" and request.headers["host"] == "www.goodreads.com"
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.get(
            "http://127.0.0.1:13379/goodreads/rss",
            headers={key: value for key, value in request.headers.items() if key.startswith("if-")},
        )
    return httpx.Response(
        response.status_code, headers=response.headers, stream=httpx.ByteStream(response.content)
    )


async def fixture_feed(url, **options):
    return await fetch_feed(
        url, **options, resolver=resolver, transport=httpx.MockTransport(handler)
    )


list_subscriptions.fetch_feed = fixture_feed
recovery_observers.fetch_feed = fixture_feed


# Keep the production lifecycle and job guards, but use one disposable worker
# across all queues. Recovery journeys inject a restore checkpoint after startup,
# so this worker also needs to consume the otherwise separate recovery queue.
async def fixture_pools(queue):
    await queue.run_worker_async(
        concurrency=1,
        shutdown_graceful_timeout=2,
        install_signal_handlers=False,
        fetch_job_polling_interval=1,
        update_heartbeat_interval=10,
        stalled_worker_timeout=60,
    )


worker.run_pools = fixture_pools
logging.basicConfig(level=logging.INFO)
asyncio.run(worker.main())
