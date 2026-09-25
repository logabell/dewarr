"""Actionable publication errors without leaking paths, tokens, or raw payloads."""

import errno

from cryptography.fernet import InvalidToken


def import_failure(error, stage):
    if isinstance(error, OSError):
        explanations = {
            errno.EXDEV: (
                "Hardlinking requires the download, staging and library folders to share "
                "one filesystem and container mount. Check the folder mappings, then "
                "retry."
            ),
            errno.EACCES: (
                "The worker cannot read the download or write to the library. Give its "
                "user access to these folders, then retry."
            ),
            errno.EPERM: (
                "The filesystem denied this operation. Check download ownership, shared- "
                "group permissions and hardlink support, then retry."
            ),
            errno.EROFS: (
                "A required folder is mounted read-only. Check the library and staging "
                "mounts, then retry."
            ),
            errno.ENOENT: (
                "A required file or folder is missing. Check the completed download path "
                "and container mounts, then retry."
            ),
            errno.ENOSPC: "The destination has no space available. Free space, then retry.",
            errno.EDQUOT: (
                "The destination storage quota has been reached. Increase it or free "
                "space, then retry."
            ),
            errno.EEXIST: (
                "A file already occupies the destination. Review Files & naming before retrying."
            ),
            errno.EIO: (
                "Storage reported an I/O error. Check the download and library storage, then retry."
            ),
        }
        code = errno.errorcode.get(error.errno, "IO_ERROR")
        explanation = explanations.get(
            error.errno,
            "Storage could not complete the operation. Check the worker logs, then retry.",
        )
        return f"{stage}: {explanation} ({code})"
    if isinstance(error, InvalidToken) or (
        isinstance(error, KeyError) and stage == "Checking import configuration"
    ):
        return (
            f"{stage}: Saved connection details are unavailable. "
            "Reconnect the library server in Settings, then retry."
        )
    return (
        f"{stage}: The saved import data could not be read. "
        "Check the worker logs for this import before retrying."
    )
