export function connectionLabel(status?: string) {
  const labels: Record<string, string> = {
    connected: "Connected",
    stale: "Check overdue",
    "not-configured": "Not connected",
    untested: "Not tested",
    disabled: "Disabled",
    authentication: "Check credentials",
    parser: "Unexpected response",
    unavailable: "Unavailable",
    timeout: "Timed out",
  };
  return status
    ? labels[status] || status.replaceAll("_", " ").replaceAll("-", " ")
    : "Not connected";
}
