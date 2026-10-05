import { type LinkProps } from "react-router-dom";

import { ContextLink } from "./NavigationContinuity";

// Keep download buttons outside the navigation link while preserving a full-card target.
export default function BookLink({ children, className, ...props }: LinkProps) {
  return (
    <div className={`book-link ${className || ""}`}>
      <ContextLink {...props} className="book-link-target" />
      {children}
    </div>
  );
}
