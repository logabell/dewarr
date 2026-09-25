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
    allowed = configuration.get("download_routes")
    if allowed is None:
        allowed = [{key: configuration.get(key) for key in ("source_key", "source_path")}]
    for receipt in receipts(probe):
        current = scope(receipt)
        if mapping and (
            current["source_key"] != mapping["source_key"]
            or (
                "relative_path" in current
                and current["relative_path"] != mapping.get("relative_path")
            )
        ):
            continue
        if any(all(current.get(key) == value for key, value in item.items()) for item in allowed):
            return True
    return False
