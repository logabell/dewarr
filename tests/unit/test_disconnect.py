import asyncio

from app.disconnect import CancelOnDisconnect


def scope(method="GET", path="/api/requests"):
    return {"type": "http", "method": method, "path": path}


async def run(method, path="/api/requests"):
    state = {"finished": False, "cancelled": False}

    async def slow(scope, receive, send):
        await receive()
        try:
            await asyncio.sleep(5)
            state["finished"] = True
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    sent = [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive():
        if sent:
            return sent.pop()
        await asyncio.sleep(0.05)  # The browser leaves shortly after asking.
        return {"type": "http.disconnect"}

    async def send(message):
        pass

    await asyncio.wait_for(CancelOnDisconnect(slow)(scope(method, path), receive, send), 1)
    return state


async def test_a_page_read_stops_when_the_browser_leaves():
    assert await run("GET") == {"finished": False, "cancelled": True}


async def test_writes_and_non_api_requests_are_left_alone():
    for method, path in (
        ("POST", "/api/requests"),
        ("GET", "/assets/app.js"),
        ("GET", "/api/books/1/sources"),  # Searches outside services.
        ("GET", "/api/requestsx"),
    ):
        try:
            await asyncio.wait_for(run(method, path), 0.2)
        except TimeoutError:
            continue  # Still running after the browser left: not cancelled.
        raise AssertionError(f"{method} {path} was cancelled")


async def test_clean_up_after_the_answer_is_not_cancelled():
    state = {"cleaned_up": False}

    async def answers_then_cleans_up(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
        await asyncio.sleep(0.1)  # Closing the database session after the answer.
        state["cleaned_up"] = True

    sent = [{"type": "http.request", "body": b"", "more_body": False}]
    complete = asyncio.Event()

    async def receive():
        if sent:
            return sent.pop()
        await complete.wait()
        return {"type": "http.disconnect"}  # Servers report this once the answer is complete.

    async def send(message):
        if message["type"] == "http.response.body" and not message.get("more_body"):
            complete.set()

    await asyncio.wait_for(CancelOnDisconnect(answers_then_cleans_up)(scope(), receive, send), 1)
    assert state["cleaned_up"]
