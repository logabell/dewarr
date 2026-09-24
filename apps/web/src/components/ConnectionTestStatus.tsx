import { CircleCheck } from "lucide-react";

type ConnectionTestStatusProps = {
  configured: boolean;
  status: string;
  lastSuccessAt?: string | null;
  isPending: boolean;
  error?: Error | null;
};

export default function ConnectionTestStatus({
  configured,
  status,
  lastSuccessAt,
  isPending,
  error,
}: ConnectionTestStatusProps) {
  const connected = status === "connected";
  const state = isPending
    ? "pending"
    : error
      ? "failed"
      : connected
        ? "connected"
        : configured && status === "untested"
          ? "saved"
          : configured
            ? "attention"
            : "not-saved";
  const label = {
    pending: "Testing…",
    failed: "Test failed",
    connected: "Saved & connected",
    saved: "Saved · not tested",
    attention: "Needs attention",
    "not-saved": "Not saved",
  }[state];
  const detail = isPending
    ? "Checking the saved connection."
    : error
      ? "Saved credentials were kept."
      : connected && lastSuccessAt
        ? `Verified ${new Date(lastSuccessAt).toLocaleString()}`
        : connected
          ? "Connection verified successfully."
          : configured && status === "untested"
            ? "Test the saved connection to verify access."
            : configured
              ? "Review the error, then test again."
              : "Save this connection before testing.";

  return (
    <span
      className="connection-test-status"
      data-state={state}
      role="status"
      aria-label="Connection test status"
      aria-live="polite"
    >
      <span className="connection-test-status-label">
        {state === "connected" && <CircleCheck size={15} aria-hidden="true" />}
        {label}
      </span>
      <span className="connection-test-status-detail">{detail}</span>
    </span>
  );
}

export function SavedSecretIndicator({ saved }: { saved: boolean }) {
  return saved ? (
    <span className="saved-secret-indicator" aria-label="Credential saved">
      Saved · ••••••••
    </span>
  ) : null;
}
