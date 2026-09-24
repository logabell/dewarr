import { CircleCheck, CircleAlert, LoaderCircle } from "lucide-react";
import { connectionLabel } from "../pages/settingLabels";

/** Shared connection feedback for settings and onboarding. */
export default function ConnectionStatus({ status }: { status?: string }) {
  const connected = status === "connected";
  const checking = status === "checking";
  const attention = Boolean(
    status &&
    ![
      "connected",
      "checking",
      "not-configured",
      "untested",
      "disabled",
    ].includes(status),
  );
  const Icon = connected
    ? CircleCheck
    : checking
      ? LoaderCircle
      : attention
        ? CircleAlert
        : null;
  return (
    <span
      className="connection-state"
      data-state={connected ? "connected" : attention ? "attention" : "neutral"}
      role="status"
    >
      {Icon && <Icon size={15} aria-hidden="true" />}
      {checking ? "Checking…" : connectionLabel(status)}
    </span>
  );
}
