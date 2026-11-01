import { expect, test } from "../fixtures";

test("release notes keep numbered lists and wrapped items, without a repeated title", async ({
  page,
}) => {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "member",
          display_name: "Reader",
          permissions: [],
          access_label: "Member",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    if (path === "/api/setup/onboarding") data = { status: "completed" };
    if (path === "/api/application/release")
      data = {
        installed_version: "v1.9.0",
        installed_url: "https://github.com/example/books/releases/tag/v1.9.0",
        latest_version: "v1.9.0",
        release_url: "https://github.com/example/books/releases/tag/v1.9.0",
        update_available: false,
        status: "checked",
      };
    if (path === "/api/application/releases")
      data = {
        installed_version: "v1.9.0",
        status: "checked",
        releases: [
          {
            version: "v1.9.0",
            name: "Dewarr v1.9.0",
            notes: [
              "# Dewarr v1.9.0",
              "",
              "## Features",
              "",
              "- Reading lists for each follow, with a",
              "  progress bar.",
              "",
              "## Upgrading",
              "",
              "1. Back up the database.",
              "2. Pull the new image.",
            ].join("\n"),
            url: "https://github.com/example/books/releases/tag/v1.9.0",
            published_at: "2026-09-21T13:58:30Z",
            prerelease: false,
          },
        ],
      };
    if (
      new URL(route.request().url()).pathname.includes(
        "/acquisition/preferences/",
      )
    )
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
  await page.goto("/settings");
  await page
    .getByRole("group", { name: "Application version" })
    .getByRole("button", { name: "v1.9.0", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "Release notes" });
  await expect(
    dialog.getByRole("heading", { name: "Dewarr v1.9.0" }),
  ).toHaveCount(1);
  await expect(dialog.locator(".release-notes ul > li")).toHaveText([
    "Reading lists for each follow, with a progress bar.",
  ]);
  const numbered = dialog.locator(".release-notes ol");
  await expect(numbered.getByRole("listitem")).toHaveText([
    "Back up the database.",
    "Pull the new image.",
  ]);
  await expect(numbered).toHaveCSS("list-style-type", "decimal");
  await expect(dialog.getByRole("heading", { name: "Features" })).toHaveCSS(
    "font-weight",
    "650",
  );
});
