import type { components } from "../api/schema";

export function transferSize(bytes: number) {
  const units = ["bytes", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(
    Math.floor(Math.log(Math.max(bytes, 1)) / Math.log(1024)),
    4,
  );
  return `${(bytes / 1024 ** index).toLocaleString(undefined, { maximumFractionDigits: 2 })} ${units[index]}`;
}

export default function DownloadConstraints({
  value,
}: {
  value?: components["schemas"]["DownloadConstraints"] | null;
}) {
  if (!value || !value.blocked_formats?.length) return null;
  return (
    <p className="muted">
      Download restrictions:{" "}
      {[
        value.blocked_formats?.length
          ? `exclude ${value.blocked_formats.map((format) => format.toUpperCase()).join(", ")}`
          : null,
      ]
        .filter(Boolean)
        .join(" · ")}
      . Books already in your library remain available.
    </p>
  );
}
