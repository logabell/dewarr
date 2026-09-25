import { expect, test } from "../fixtures";

for (const state of ["update", "current", "offline", "unconfigured"]) {
  test(`sidebar release tracker: ${state}`, async ({ page }) => {
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
          installed_url:
            state === "unconfigured"
              ? null
              : "https://github.com/example/books/releases/tag/v1.9.0",
          latest_version: state === "update" ? "v1.10.0" : "v1.9.0",
          release_url: "https://github.com/example/books/releases/tag/v1.10.0",
          update_available: state === "update",
          status:
            state === "offline"
              ? "unavailable"
              : state === "unconfigured"
                ? "unconfigured"
                : "checked",
        };
      if (path === "/api/application/releases")
        data = {
          installed_version: "v1.9.0",
          status:
            state === "offline"
              ? "unavailable"
              : state === "unconfigured"
                ? "unconfigured"
                : "checked",
          releases:
            state === "offline" || state === "unconfigured"
              ? []
              : [
                  {
                    version: state === "update" ? "v1.10.0" : "v1.9.0",
                    name:
                      state === "update" ? "Dewarr v1.10.0" : "Dewarr v1.9.0",
                    notes:
                      state === "update"
                        ? "## Features\n\n- Reading lists\n"
                        : "Current release notes.",
                    url: `https://github.com/example/books/releases/tag/${state === "update" ? "v1.10.0" : "v1.9.0"}`,
                    published_at: "2026-09-21T13:58:30Z",
                    prerelease: false,
                  },
                  {
                    version: "v1.8.0",
                    name: "Dewarr v1.8.0",
                    notes: "Earlier fixes.",
                    url: "https://github.com/example/books/releases/tag/v1.8.0",
                    published_at: "2026-08-01T00:00:00Z",
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
    const footer = page.getByRole("group", {
      name: "Application version",
    });
    await expect(footer).toBeVisible();
    await expect(footer.getByText("v1.9.0", { exact: true })).toBeVisible();
    if (state === "update") {
      await footer.getByRole("button", { name: "Update", exact: true }).click();
      const notes = page.getByRole("dialog", { name: "Release notes" });
      await expect(notes.getByText("v1.10.0 is available.")).toBeVisible();
      await expect(
        notes.getByRole("heading", { name: "Dewarr v1.10.0" }),
      ).toBeVisible();
      await expect(
        notes.getByRole("heading", { name: "Features" }),
      ).toBeVisible();
      await expect(notes.getByText("Reading lists")).toBeVisible();
      await expect(
        notes.getByRole("heading", { name: "Earlier releases" }),
      ).toBeVisible();
      await expect(notes.getByText("Earlier fixes.")).toBeVisible();
      await expect(
        notes.getByRole("link", { name: "View on GitHub" }).first(),
      ).toHaveAttribute(
        "href",
        "https://github.com/example/books/releases/tag/v1.10.0",
      );
      await notes.getByRole("button", { name: "Close release notes" }).click();
      await expect(notes).toHaveCount(0);
      const sidebar = await page.locator(".sidebar").boundingBox();
      const bounds = await footer.boundingBox();
      expect(bounds!.y).toBeGreaterThan(sidebar!.height - 150);
    } else if (state === "current") {
      await expect(footer.getByText("Up to date")).toBeVisible();
      await footer.getByRole("button", { name: "v1.9.0", exact: true }).click();
      const notes = page.getByRole("dialog", { name: "Release notes" });
      await expect(
        notes.getByText("This installation is up to date."),
      ).toBeVisible();
      await expect(
        notes.getByRole("heading", { name: "Dewarr v1.9.0" }),
      ).toBeVisible();
      await expect(notes.getByText("Current release notes.")).toBeVisible();
      await notes.getByRole("button", { name: "Close release notes" }).click();
    } else {
      await expect(
        footer.getByRole("button", { name: "Update", exact: true }),
      ).toHaveCount(0);
      await expect(
        footer.getByText(
          state === "current"
            ? "Up to date"
            : state === "offline"
              ? "Update check unavailable"
              : "Release tracking not configured",
        ),
      ).toBeVisible();
      await footer.getByRole("button", { name: "v1.9.0", exact: true }).click();
      const notes = page.getByRole("dialog", { name: "Release notes" });
      await expect(
        notes.getByText(
          state === "offline"
            ? "Release notes could not be loaded from GitHub."
            : "Release tracking is not configured, so notes from GitHub are unavailable.",
        ),
      ).toBeVisible();
      await notes.getByRole("button", { name: "Close release notes" }).click();
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(footer).toBeVisible();
    if (state === "update") {
      await footer.getByRole("button", { name: "Update", exact: true }).click();
      const notes = page.getByRole("dialog", { name: "Release notes" });
      await expect(
        notes.getByRole("heading", { name: "Features" }),
      ).toBeVisible();
      const box = await notes.boundingBox();
      expect(box!.width).toBeLessThanOrEqual(390);
      await notes.getByRole("button", { name: "Close release notes" }).click();
    }
  });
}
