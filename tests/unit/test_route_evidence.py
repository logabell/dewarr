import pytest

from app.importing.route_evidence import backend_verified


@pytest.mark.parametrize(
    "probe, expected",
    [
        (None, False),
        ({}, False),
        ({"backend": {}}, False),
        ({"backend": {"configuration_validated": True}}, True),
        ({"backend": {"root_mapping": True}}, True),
        ({"backend": {"root_mapping": False}}, False),
        ({"backend": {"configuration_validated": False, "root_mapping": True}}, False),
    ],
)
def test_backend_receipts_accept_legacy_success_without_overriding_new_failure(probe, expected):
    assert backend_verified(probe) is expected
