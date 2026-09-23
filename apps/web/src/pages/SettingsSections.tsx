import { lazy } from "react";

const Logs = lazy(() => import("./Activity"));
const Display = lazy(() => import("./DisplaySettings"));
const ReadingAccounts = lazy(() => import("./ReadingAccounts"));
const Metadata = lazy(() => import("./MetadataSettings"));
const Libraries = lazy(() => import("./Connections"));
const Sources = lazy(() => import("./SourceSettings"));
const Downloaders = lazy(() => import("./Downloaders"));
const Preferences = lazy(() => import("./DownloadPreferences"));
const Naming = lazy(() => import("./Organization"));
const Recovery = lazy(() => import("./DownloadRecoverySettings"));
const Accounts = lazy(() => import("./Accounts"));

export function settingsSections(role: string, permissions: string[] = []) {
  const admin = role === "admin";
  const manageUsers = admin || permissions.includes("manage_users");
  return [
    { id: "display", title: "General", content: <Display /> },
    {
      id: "catalog",
      title: "Metadata",
      content: <Metadata admin={admin} embedded />,
    },
    ...(role !== "viewer"
      ? [
          {
            id: "reading",
            title: "Reading accounts",
            content: <ReadingAccounts />,
          },
        ]
      : []),
    ...(!admin && role !== "viewer"
      ? [
          {
            id: "libraries",
            title: "Libraries",
            content: <Preferences admin={false} embedded librariesOnly />,
          },
        ]
      : []),
    ...(admin
      ? [
          {
            id: "libraries",
            title: "Libraries",
            content: <Libraries embedded />,
          },
          { id: "sources", title: "Download sources", content: <Sources /> },
          { id: "recovery", title: "Download recovery", content: <Recovery /> },
          {
            id: "downloaders",
            title: "Download clients",
            content: <Downloaders embedded />,
          },
        ]
      : []),
    ...(role !== "viewer"
      ? [
          {
            id: "preferences",
            title: "Download preferences",
            content: <Preferences admin={admin} embedded />,
          },
        ]
      : []),
    ...(admin
      ? [{ id: "naming", title: "File naming", content: <Naming embedded /> }]
      : []),
    ...(manageUsers
      ? [
          {
            id: "accounts",
            title: "Users & access",
            content: <Accounts embedded />,
          },
        ]
      : []),
    { id: "logs", title: "Logs", content: <Logs admin={admin} /> },
  ];
}
