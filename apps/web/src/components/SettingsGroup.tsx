import { Suspense, useEffect, useState, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import { Loading } from "../components";
import "./settings-groups.css";

/** Load a settings group on first use and retain its draft when collapsed. */
export default function SettingsGroup({
  id,
  title,
  description,
  children,
}: {
  id: string;
  title: string;
  description: string;
  children: ReactNode;
}) {
  const location = useLocation();
  const linked = new URLSearchParams(location.search).get("group") === id;
  const [open, setOpen] = useState(linked);
  const [visited, setVisited] = useState(linked);
  useEffect(() => {
    if (linked) {
      setOpen(true);
      setVisited(true);
    }
  }, [linked]);
  return (
    <details
      className="settings-group"
      open={open}
      onToggle={(event) => {
        setOpen(event.currentTarget.open);
        if (event.currentTarget.open) setVisited(true);
      }}
    >
      <summary>
        <span className="settings-group-heading">
          <span className="settings-group-title">{title}</span>
          <span className="settings-group-description">{description}</span>
        </span>
      </summary>
      {visited && (
        <div className="settings-group-content">
          <Suspense fallback={<Loading />}>{children}</Suspense>
        </div>
      )}
    </details>
  );
}
