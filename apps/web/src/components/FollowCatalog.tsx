import { Link } from "react-router-dom";

export default function FollowCatalog({
  kind,
  externalId,
  name,
}: {
  kind: "author" | "series";
  externalId: string;
  name: string;
}) {
  const query = new URLSearchParams({ kind, externalId, name });
  return (
    <Link className="reader-action-link" to={`/following?${query}`}>
      {kind === "author" ? "Follow author" : "Follow future additions"}
    </Link>
  );
}
