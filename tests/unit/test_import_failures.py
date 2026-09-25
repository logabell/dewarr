import errno

import pytest
from cryptography.fernet import InvalidToken

from app.importing.failures import import_failure


@pytest.mark.parametrize(
    "code",
    [errno.EXDEV, errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOENT, errno.ENOSPC, errno.EIO],
)
def test_storage_failure_explains_action_without_leaking_path(code):
    message = import_failure(OSError(code, "private error", "/private/token"), "Organizing files")
    assert errno.errorcode[code] in message
    assert message.startswith("Organizing files:")
    assert "/private" not in message and "private error" not in message
    assert len(message) <= 500


@pytest.mark.parametrize(
    "error", [ValueError("secret input"), KeyError("secret key"), InvalidToken("secret token")]
)
def test_configuration_failure_never_exposes_raw_error(error):
    assert "secret" not in import_failure(error, "Checking configuration")
