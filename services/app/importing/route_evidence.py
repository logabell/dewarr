"""Independent download-path receipts for a shared final library destination."""


def receipts(probe):
    if not probe:
        return []
    return probe.get("download_routes", [probe])


def scope(probe):
    result = {"source_key": probe.get("source_key"), "source_path": probe.get("source_path")}
    binding = probe.get("setup_downloader")
    if binding:
        result["relative_path"] = binding["mapping"]["relative_path"]
    return result


def approval(probe):
    result = {"source_key": probe["source_key"], "source_path": probe["source_path"]}
    if "download_routes" in probe:
        result["download_routes"] = [scope(item) for item in receipts(probe)]
    return result


def approved(configuration, probe, mapping=None):
    """An enabled library policy covers every currently verified download folder.

    Saved source scopes describe the original approval, not a second client allowlist.
    Callers validate the policy's owner, enabled state, generation and destination;
    current probe receipts still bind each client's path and configuration.
    """
    for receipt in receipts(probe):
        if receipt.get("status") != "verified" or receipt.get(
            "configuration_revision"
        ) != configuration.get("destination_revision"):
            continue
        current = scope(receipt)
        if mapping and (
            current["source_key"] != mapping["source_key"]
            or (
                "relative_path" in current
                and current["relative_path"] != mapping.get("relative_path")
            )
        ):
            continue
        return True
    return False
