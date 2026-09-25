from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from pydantic import ValidationError

from app.adapters.mam import AccountAutomation, MAMClient
from app.adapters.mam_account import MAMPurchase, account_data
from app.domain.account_automation import due_command


def command(kind="upload", amount=50):
    return MAMPurchase(
        request_id=uuid4(),
        expected_generation=1,
        kind=kind,
        **({"amount": amount} if kind == "upload" else {}),
    )


@pytest.mark.parametrize("amount", [0, 49, 2.5, True, "50", "Max Affordable ", 100001])
def test_purchase_rejects_invalid_upload_amounts(amount):
    with pytest.raises(ValidationError):
        command(amount=amount)


def test_purchase_fields_do_not_accept_arbitrary_store_actions():
    for values in [
        {"kind": "gift"},
        {"kind": "VIP", "amount": 50},
        {"kind": "upload"},
        {"kind": "VIP", "duration": 4},
    ]:
        with pytest.raises(ValidationError):
            MAMPurchase(request_id=uuid4(), expected_generation=1, **values)
    assert AccountAutomation(ratio_buy_gb="max").ratio_buy_gb == "max"


def test_account_fields_are_allowlisted_and_unknown_is_not_zero():
    account = account_data(
        {
            "uid": 9,
            "username": "Mouse",
            "ratio": "∞",
            "uploaded": "1.25 TiB",
            "downloaded": "42 GiB",
            "seedbonus": "NaN",
            "secret": "private",
            "notifications": {"token": "private"},
            "vip_until": "nonsense",
        }
    )
    assert account.ratio == "∞" and account.uploaded == "1.25 TiB"
    assert account.seedbonus is None and account.classname is None and account.vip_until is None
    assert "private" not in account.model_dump_json()


@pytest.mark.parametrize(
    "kind,amount,extra",
    [
        ("upload", 80, {"amount": "80"}),
        ("upload", "max", {"amount": "Max Affordable "}),
        ("VIP", None, {"duration": "max"}),
        ("wedges", None, {}),
    ],
)
async def test_store_parameters_and_cookie_rotation(kind, amount, extra):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("jsonLoad.php"):
            return httpx.Response(
                200,
                json={"uid": 9, "username": "Mouse", "seedbonus": 100000},
                headers={"Set-Cookie": "mam_id=rotated; Path=/"},
            )
        assert request.headers["cookie"] == "mam_id=rotated"
        return httpx.Response(200, json={"Success": True, "secret": "do-not-return"})

    async with MAMClient(
        "https://mam.test", "session", transport=httpx.MockTransport(handler), request_interval=0
    ) as client:
        result = await client.purchase(command(kind, amount))
    assert result.status == "completed" and "do-not-return" not in result.model_dump_json()
    params = dict(calls[1].url.params)
    params.pop("_")
    assert params == {"spendtype": kind, **extra}
    assert len(calls) == 2


@pytest.mark.parametrize("balance", [24999, None, "Infinity"])
async def test_unaffordable_purchase_never_calls_store(balance):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"uid": 9, "username": "Mouse", "seedbonus": balance})

    async with MAMClient(
        "https://mam.test", "session", transport=httpx.MockTransport(handler), request_interval=0
    ) as client:
        result = await client.purchase(command())
    assert result.status == "rejected" and len(calls) == 1


@pytest.mark.parametrize(
    "reply,status",
    [({"success": False}, "rejected"), ({}, "unknown"), ({"success": True}, "completed")],
)
async def test_purchase_result_requires_explicit_acknowledgement(reply, status):
    def handler(request):
        return httpx.Response(
            200,
            json=reply
            if request.url.path.endswith("bonusBuy.php")
            else {"uid": 9, "username": "Mouse", "seedbonus": 50000},
        )

    async with MAMClient(
        "https://mam.test", "session", transport=httpx.MockTransport(handler), request_interval=0
    ) as client:
        assert (await client.purchase(command())).status == status


async def test_store_timeout_is_unknown_and_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("bonusBuy.php"):
            raise httpx.ReadTimeout("lost response", request=request)
        return httpx.Response(200, json={"uid": 9, "username": "Mouse", "seedbonus": 50000})

    async with MAMClient(
        "https://mam.test", "session", transport=httpx.MockTransport(handler), request_interval=0
    ) as client:
        assert (await client.purchase(command())).status == "unknown"
    assert len(calls) == 2


def test_seedbox_legacy_intervals_respect_hourly_limit():
    now = datetime.now(UTC)
    assert (
        due_command(
            {"seedbox_ip": True, "seedbox_interval_seconds": 60},
            {"seedbox_at": (now - timedelta(minutes=59)).isoformat()},
            now,
        )
        is None
    )
    assert due_command(
        {"seedbox_ip": True, "seedbox_interval_seconds": 60},
        {"seedbox_at": (now - timedelta(hours=1)).isoformat()},
        now,
    ).seedbox


async def test_automation_max_only_buys_once_even_when_store_omits_new_balance():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"success": True}
            if request.url.path.endswith("bonusBuy.php")
            else {"uid": 9, "username": "Mouse", "seedbonus": 100000, "ratio": 1},
        )

    async with MAMClient(
        "https://mam.test", "session", transport=httpx.MockTransport(handler), request_interval=0
    ) as client:
        result = await client.maintain(
            {"upload_ratio": True, "ratio_buy_gb": "max", "upload_bonus": True}
        )
    assert result.upload_purchased and len(calls) == 2
    assert calls[-1].url.params["amount"] == "Max Affordable "


async def test_automation_unacknowledged_ratio_purchase_stops_bonus_rule():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={}
            if request.url.path.endswith("bonusBuy.php")
            else {
                "uid": 9,
                "username": "Mouse",
                "seedbonus": 100000,
                "ratio": 1,
            },
        )

    async with MAMClient(
        "https://mam.test", "session", transport=httpx.MockTransport(handler), request_interval=0
    ) as client:
        result = await client.maintain({"upload_ratio": True, "upload_bonus": True})
    assert not result.upload_purchased
    assert len(calls) == 2
