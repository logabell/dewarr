from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.adapters.contracts import AdapterError, FailureKind
from app.adapters.mam import (
    AccountAutomation,
    HelperResult,
    MAMClient,
    MAMSearch,
    download_path,
    gigabytes,
    parse_page,
    seedbox_update_target,
    spend_wedge,
    vip_weeks_available,
)
from app.domain.account_automation import due_command, next_automation_state
from tests.mam_fixture import release_row, search_response


def test_download_reference_cannot_turn_on_a_wedge_by_itself():
    with pytest.raises(AdapterError, match="unsupported options") as error:
        download_path("token?fl", "501")
    assert error.value.kind == FailureKind.UNSUPPORTED
    assert download_path("token", "501") == "tor/download.php/token?tid=501"
    assert download_path("token", "501", personal_freeleech=True).endswith("?tid=501&fl")


def test_seedbox_update_uses_the_tracker_host_only_for_mam():
    assert (
        seedbox_update_target("https://www.myanonamouse.net")
        == "https://t.myanonamouse.net/json/dynamicSeedbox.php"
    )
    assert seedbox_update_target("https://mam.test/json") == "json/dynamicSeedbox.php"


def test_wedge_is_skipped_when_the_torrent_is_already_free():
    now = datetime.now(UTC)
    item = parse_page(
        search_response(data=[release_row(free=0, personal_freeleech=0, fl_vip=0)]),
        MAMSearch(q="Harbor"),
    ).items[0]
    assert spend_wedge(item, None, False, now) is False
    assert spend_wedge(item, None, True, now) is True
    free = item.model_copy(update={"freeleech": True})
    owned = item.model_copy(update={"personal_freeleech": True})
    vip_free = item.model_copy(update={"vip_freeleech": True})
    assert spend_wedge(free, None, True, now) is False
    assert spend_wedge(owned, None, True, now) is False
    assert spend_wedge(vip_free, now + timedelta(days=7), True, now) is False
    assert spend_wedge(vip_free, now - timedelta(days=1), True, now) is True


def test_vip_and_upload_purchases_stay_inside_the_store_limits():
    assert vip_weeks_available({"seedbonus": 1249}) == 0
    assert vip_weeks_available({"seedbonus": 2500, "vip_until": "2099-01-01 00:00:00"}) == 0
    assert vip_weeks_available({"seedbonus": 2500, "vip_until": "2000-01-01 00:00:00"}) == 2
    assert gigabytes("1.25 GiB") == pytest.approx(1.25)
    assert gigabytes("512 MiB") == pytest.approx(0.5)


def test_helpers_do_nothing_until_an_administrator_enables_one():
    now = datetime(2026, 9, 21, tzinfo=UTC)
    assert due_command({}, {}, now) is None
    assert due_command({"use_wedge": True}, {}, now) is None
    assert due_command({"seedbox_ip": "yes"}, {}, now) is None
    command = due_command({"seedbox_ip": True, "auto_vip": True, "protect_ratio": True}, {}, now)
    assert command is not None
    assert command.seedbox and command.seedbox_stale and command.vip and command.upload_ratio
    assert command.ratio_below == 2.5
    assert AccountAutomation().upload_interval_hours == 3
    recent = now.isoformat()
    assert (
        due_command(
            {"seedbox_ip": True, "seedbox_interval_seconds": 300},
            {"seedbox_at": recent},
            now,
        )
        is None
    )
    assert (
        due_command(
            {"protect_ratio": True, "upload_interval_hours": 1},
            {"upload_at": (now - timedelta(minutes=30)).isoformat()},
            now,
        )
        is None
    )
    later = due_command(
        {"protect_ratio": True, "upload_interval_hours": 1, "ratio_buy_gb": 80},
        {"upload_at": (now - timedelta(hours=2)).isoformat()},
        now,
    )
    assert later is not None and later.ratio_buy_gb == 80


def test_failed_helper_backs_off_only_the_action_that_ran():
    now = datetime(2026, 9, 21, tzinfo=UTC)
    state = next_automation_state({}, {"vip": True}, None, now)
    assert state == {"vip_at": now.isoformat()}
    finished = next_automation_state(
        {},
        {"seedbox": True},
        HelperResult(
            checked=["seedbox"],
            seedbox_ip="203.0.113.10",
            seedbox_asn="64500",
            seedbox_authorized=True,
        ),
        now,
    )
    assert finished["seedbox_ip"] == "203.0.113.10"
    assert finished["seedbox_authorized_at"] == now.isoformat()


def _client(handler):
    return MAMClient(
        "https://mam.test",
        "fixture-session",
        transport=httpx.MockTransport(handler),
        request_interval=0,
    )


async def test_enabled_wedge_is_added_only_for_a_torrent_that_is_not_free():
    queries = []

    def handler(request):
        if "download.php" in request.url.path:
            queries.append(request.url.query.decode())
            return httpx.Response(200, content=b"d4:infod4:name6:Harboree")
        return httpx.Response(200, json=search_response(data=[release_row(free=0)]))

    async with _client(handler) as client:
        client.use_wedge = True
        artifact = await client.resolve("501")
    assert artifact.content.startswith(b"d")
    assert queries == ["tid=501&fl"] or queries == ["tid=501&fl="]


async def test_public_freeleech_does_not_spend_a_wedge():
    queries = []

    def handler(request):
        if "download.php" in request.url.path:
            queries.append(request.url.query.decode())
            return httpx.Response(200, content=b"d4:infod4:name6:Harboree")
        return httpx.Response(200, json=search_response(data=[release_row(free=1)]))

    async with _client(handler) as client:
        client.use_wedge = True
        await client.resolve("501")
    assert queries == ["tid=501"]


async def test_remote_freeleech_flag_is_rejected_before_the_torrent_is_fetched():
    def handler(request):
        if "download.php" in request.url.path:
            raise AssertionError("download")
        return httpx.Response(200, json=search_response(data=[release_row(dl="token?fl")]))

    async with _client(handler) as client:
        client.use_wedge = True
        with pytest.raises(AdapterError, match="unsupported options"):
            await client.resolve("501")


async def test_seedbox_check_omits_the_session_and_skips_an_unchanged_address():
    calls = []

    def handler(request):
        calls.append(
            (request.url.path, request.headers.get("cookie", ""), dict(request.url.params))
        )
        if request.url.path.endswith("jsonIp.php"):
            return httpx.Response(200, json={"ip": "203.0.113.10", "ASN": 64500})
        raise AssertionError(request.url.path)

    async with _client(handler) as client:
        result = await client.maintain(
            {
                "seedbox": True,
                "known_ip": "203.0.113.10",
                "known_asn": "64500",
                "seedbox_stale": False,
            }
        )
    assert result.seedbox_unchanged
    assert calls[0][0].endswith("jsonIp.php")
    assert "fixture-session" not in calls[0][1]


async def test_changed_seedbox_address_is_authorized_without_logging_the_ip(caplog):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("jsonIp.php"):
            return httpx.Response(200, json={"ip": "203.0.113.20", "ASN": 64500})
        if request.url.path.endswith("dynamicSeedbox.php"):
            return httpx.Response(200, json={"Success": True})
        raise AssertionError(request.url.path)

    async with _client(handler) as client:
        with caplog.at_level("INFO"):
            result = await client.maintain({"seedbox": True, "seedbox_stale": False})
    assert result.seedbox_authorized
    assert [path.rsplit("/", 1)[-1] for path in calls] == ["jsonIp.php", "dynamicSeedbox.php"]
    assert "203.0.113.20" not in caplog.text


async def test_vip_top_up_is_skipped_when_one_week_is_unaffordable():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"uid": 9, "username": "member", "seedbonus": 100, "vip_until": "2000-01-01"},
        )

    async with _client(handler) as client:
        result = await client.maintain({"vip": True})
    assert result.checked == ["vip"] and not result.vip_purchased
    assert calls == ["/jsonLoad.php"]


async def test_ratio_and_bonus_each_buy_upload_credit():
    calls = []

    def handler(request):
        calls.append(
            (
                request.url.path,
                request.url.params.get("spendtype"),
                request.url.params.get("amount"),
            )
        )
        if request.url.path.endswith("bonusBuy.php"):
            return httpx.Response(200, json={"success": True, "seedbonus": 30000})
        return httpx.Response(
            200,
            json={
                "uid": 9,
                "username": "member",
                "seedbonus": 55000,
                "ratio": 1.2,
                "uploaded": "20 GiB",
                "downloaded": "15 GiB",
            },
        )

    async with _client(handler) as client:
        result = await client.maintain({"upload_ratio": True, "upload_bonus": True})
    assert result.upload_purchased
    purchases = [call for call in calls if call[0].endswith("bonusBuy.php")]
    assert purchases == [
        ("/json/bonusBuy.php", "upload", "50"),
        ("/json/bonusBuy.php", "upload", "50"),
    ]


async def test_one_torrent_can_spend_a_wedge_without_turning_automation_on():
    queries = []

    def handler(request):
        if "download.php" in request.url.path:
            queries.append(request.url.query.decode())
            return httpx.Response(200, content=b"d4:infod4:name6:Harboree")
        return httpx.Response(200, json=search_response(data=[release_row(free=0)]))

    async with _client(handler) as client:
        await client.resolve({"source_id": "501", "use_wedge": True})
    assert queries == ["tid=501&fl"] or queries == ["tid=501&fl="]


async def test_automatic_wedge_skips_a_torrent_under_the_minimum_size():
    queries = []

    def handler(request):
        if "download.php" in request.url.path:
            queries.append(request.url.query.decode())
            return httpx.Response(200, content=b"d4:infod4:name6:Harboree")
        return httpx.Response(
            200, json=search_response(data=[release_row(free=0, size="1.25 GiB")])
        )

    async with _client(handler) as client:
        client.automation = AccountAutomation(
            use_wedge=True, wedge_min_size=True, wedge_min_size_mb=2000
        )
        await client.resolve("501")
    assert queries == ["tid=501"]


async def test_a_chosen_torrent_ignores_the_minimum_size():
    queries = []

    def handler(request):
        if "download.php" in request.url.path:
            queries.append(request.url.query.decode())
            return httpx.Response(200, content=b"d4:infod4:name6:Harboree")
        return httpx.Response(200, json=search_response(data=[release_row(free=0)]))

    async with _client(handler) as client:
        client.automation = AccountAutomation(
            use_wedge=True, wedge_min_size=True, wedge_min_size_mb=9000
        )
        await client.resolve({"source_id": "501", "use_wedge": True})
    assert queries == ["tid=501&fl"] or queries == ["tid=501&fl="]


async def test_vip_only_torrent_is_not_fetched_without_active_vip():
    def handler(request):
        if request.url.path.endswith("jsonLoad.php"):
            return httpx.Response(200, json={"vip_until": "2000-01-01 00:00:00"})
        if "download.php" in request.url.path:
            raise AssertionError("download")
        return httpx.Response(200, json=search_response(data=[release_row(vip=1, free=0)]))

    async with _client(handler) as client:
        with pytest.raises(AdapterError, match="active MyAnonamouse VIP") as error:
            await client.resolve("501")
    assert error.value.kind == FailureKind.UNSUPPORTED


async def test_bonus_purchases_stop_when_points_fall_under_the_threshold():
    amounts = []

    def handler(request):
        if request.url.path.endswith("bonusBuy.php"):
            amounts.append(request.url.params.get("amount"))
            remaining = 4000 if len(amounts) > 1 else 29000
            return httpx.Response(200, json={"success": True, "seedbonus": remaining})
        return httpx.Response(
            200,
            json={
                "uid": 9,
                "username": "member",
                "seedbonus": 54000,
                "ratio": 2,
                "uploaded": "20 GiB",
                "downloaded": "5 GiB",
            },
        )

    async with _client(handler) as client:
        result = await client.maintain({"upload_bonus": True, "bonus_above": 5000})
    assert result.upload_purchased
    assert amounts == ["50", "50"]
