import { expect, test } from "./fixtures";

for (const kind of ["downloader", "library", "folder", "source"] as const) {
  test(`${kind} deletion requires confirmation and supports retry after conflict`, async ({
    page,
  }, testInfo) => {
    let removed = false;
    let conflict = true;
    const deletes: string[] = [];
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/**", (route) => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (route.request().method() === "DELETE") {
        deletes.push(path);
        expect(route.request().headers()["x-csrf-token"]).toBe("test");
        if (kind === "downloader" || kind === "source")
          expect(url.searchParams.get("expected_generation")).toBe("4");
        if (kind === "folder")
          expect(url.searchParams.get("expected_revision")).toBe("revision");
        if (conflict)
          return route.fulfill({
            status: 409,
            json: {
              detail:
                "Unfinished downloads use this configuration. Finish or cancel them before deleting.",
            },
          });
        removed = true;
        return route.fulfill({ status: 204 });
      }
      let data: unknown = [];
      if (path === "/api/auth/me")
        data = {
          user: {
            id: "admin",
            role: "admin",
            display_name: "Admin",
            onboarding_status: "complete",
          },
          csrf_token: "test",
        };
      else if (path === "/api/setup/onboarding") data = { status: "completed" };
      else if (path === "/api/downloaders")
        data = removed
          ? []
          : [
              {
                id: "client",
                kind: "qbittorrent",
                name: "Test client",
                base_url: "http://qbit.test",
                enabled: true,
                status: "connected",
                generation: 4,
                mappings: [],
                mappings_current: false,
                save_path: "",
                category: "",
                capabilities: {},
                limitations: [],
              },
            ];
      else if (path === "/api/integrations")
        data = removed
          ? []
          : [
              {
                id: "library",
                kind: "audiobookshelf",
                name: "Test library",
                base_url: "http://abs.test",
                enabled: true,
                status: "connected",
              },
            ];
      else if (path === "/api/organization/destinations")
        data = removed
          ? []
          : [
              {
                id: "folder",
                library_id: "lib",
                root_key: "library-ebook",
                medium: "ebook",
                backend_path: "/data/ebooks",
                mode: "hardlink",
                revision: "revision",
                enabled: true,
                server_kind: "audiobookshelf",
              },
            ];
      else if (path.endsWith("/automatic-import"))
        data = { enabled: false, can_enable: false };
      else if (path.startsWith("/api/acquisition/preferences/"))
        data = {
          effective: {},
          overrides: {},
          inherited: {},
          revision: "revision",
        };
      else if (path === "/api/sources/prowlarr/connection")
        data = {
          configured: !removed,
          enabled: !removed,
          generation: removed ? 5 : 4,
          base_url: removed ? "" : "http://prowlarr.test",
          excluded_indexers: [],
          status: removed ? "not-configured" : "connected",
          has_api_key: !removed,
        };
      else if (path.startsWith("/api/sources/") && path.endsWith("/connection"))
        data = {
          configured: false,
          enabled: false,
          generation: 0,
          base_url: "",
          status: "not-configured",
          automation: {},
        };
      return route.fulfill({ json: data });
    });
    const name = {
      downloader: "Test client",
      library: "Test library",
      folder: "ebooks folder",
      source: "Prowlarr",
    }[kind];
    const section = {
      downloader: "downloaders",
      library: "libraries",
      folder: "libraries",
      source: "sources",
    }[kind];
    await page.goto(`/settings#${section}`);
    if (kind === "source")
      await page.getByText("Prowlarr", { exact: true }).click();
    const trigger = page.getByRole("button", {
      name: `Delete ${name}`,
      exact: true,
    });
    await trigger.click();
    const dialog = page.getByRole("dialog", {
      name: `Delete ${name}?`,
      exact: true,
    });
    await expect(dialog).toBeVisible();
    if (kind === "downloader") {
      await dialog.screenshot({
        path: testInfo.outputPath("delete-confirmation-desktop.png"),
      });
      await page.setViewportSize({ width: 390, height: 844 });
      await expect(dialog).toBeVisible();
      await dialog.screenshot({
        path: testInfo.outputPath("delete-confirmation-mobile.png"),
      });
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.setViewportSize({ width: 1440, height: 1000 });
    }
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    expect(deletes).toEqual([]);
    await expect(trigger).toBeFocused();
    await trigger.click();
    await dialog.getByRole("button", { name: "Delete", exact: true }).click();
    await expect(dialog).toContainText("Unfinished downloads");
    expect(removed).toBe(false);
    conflict = false;
    await dialog.getByRole("button", { name: "Delete", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await expect(trigger).toHaveCount(0);
    expect(deletes).toHaveLength(2);
    if (kind === "source")
      await expect(
        page
          .getByRole("form", { name: "Prowlarr connection settings" })
          .getByLabel("Server URL"),
      ).toHaveValue("");
    expect(errors).toEqual([]);
  });
}
