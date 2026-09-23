import { expect, test } from "./fixtures";

for (const role of ["admin", "member", "viewer"]) {
  test(`${role}: focused settings, requests, queue, and log deep links`, async ({
    page,
  }, testInfo) => {
    const errors: string[] = [];
    const endpoints: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/**", (route) => {
      const url = new URL(route.request().url());
      endpoints.push(url.pathname);
      let data: unknown = [];
      if (url.pathname === "/api/auth/me")
        data = {
          user: {
            id: "reader",
            role,
            display_name: "Reader",
            onboarding_status: "complete",
            permissions: [],
          },
          csrf_token: "test",
        };
      else if (url.pathname === "/api/setup/onboarding")
        data = { status: "completed" };
      else if (
        url.pathname === "/api/requests" ||
        url.pathname === "/api/acquisition/downloads"
      )
        data = { items: [], total: 0 };
      else if (url.pathname.startsWith("/api/acquisition/preferences/"))
        data = {
          revision: "one",
          overrides: {},
          inherited: {},
          inherited_origins: {},
        };
      else if (url.pathname === "/api/activity/page")
        data = {
          items: [],
          total: 0,
          statuses: ["completed", "failed"],
          kinds: ["library.sync"],
        };
      return route.fulfill({ json: data });
    });
    await page.goto("/settings");
    await expect(
      page.getByRole("region", { name: "General", exact: true }),
    ).toBeVisible();
    expect(endpoints).not.toContain("/api/downloaders");
    expect(endpoints).not.toContain("/api/activity/page");
    const categories = page.getByRole("navigation", {
      name: "Settings categories",
    });
    const tabTops = await categories
      .getByRole("link")
      .evaluateAll((links) =>
        links.map((link) => Math.round(link.getBoundingClientRect().top)),
      );
    expect(new Set(tabTops).size).toBe(1);
    await expect(
      categories.getByRole("link", { name: "Library folders" }),
    ).toHaveCount(0);
    await expect(
      categories.getByRole("link", { name: "Saved profiles", exact: true }),
    ).toHaveCount(0);
    await page.goto("/settings#profiles");
    await expect(page).toHaveURL(
      role === "viewer" ? /#display$/ : /#preferences$/,
    );
    await expect(
      categories.getByRole("link", { name: "Download clients" }),
    ).toHaveCount(role === "admin" ? 1 : 0);
    if (role === "admin") {
      await page.goto("/settings#storage");
      await expect(
        page.getByRole("button", { name: "Choose ebooks folder" }),
      ).toBeVisible();
      await expect(
        categories.getByRole("link", { name: "Libraries", exact: true }),
      ).toHaveAttribute("aria-current", "page");
      await expect(
        page.getByRole("region", { name: "General", exact: true }),
      ).toHaveCount(0);
    }
    await categories.getByRole("link", { name: "Logs", exact: true }).click();
    await page
      .getByRole("combobox", { name: "Activity status", exact: true })
      .selectOption("failed");
    await expect(page).toHaveURL(/status=failed#logs$/);
    await page.reload();
    await expect(
      page.getByRole("combobox", { name: "Activity status", exact: true }),
    ).toHaveValue("failed");
    await expect(
      page.getByRole("button", { name: "Check background worker" }),
    ).toHaveCount(role === "admin" ? 1 : 0);
    await page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: "Requests", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "Requests", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Background activity" }),
    ).toHaveCount(0);
    const requestFilters = page.getByRole("navigation", {
      name: "Request filters",
    });
    await requestFilters
      .getByRole("link", { name: "Downloading", exact: true })
      .click();
    await expect(
      page.getByText("No downloads yet.", { exact: true }),
    ).toBeVisible();
    await expect(
      requestFilters.getByRole("link", { name: "Review", exact: true }),
    ).toHaveCount(role === "admin" ? 1 : 0);
    await page.goto("/activity#downloads");
    await expect(page).toHaveURL(/\/requests#downloads$/);
    await page.goto("/activity?status=completed");
    await expect(page).toHaveURL(/\/settings\?status=completed#logs$/);
    await expect(
      page.getByRole("combobox", { name: "Activity status", exact: true }),
    ).toHaveValue("completed");
    await page.screenshot({ path: testInfo.outputPath("logs-desktop.png") });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: testInfo.outputPath("logs-mobile.png") });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.goto("/requests#downloads");
    await expect(
      page.getByText("No downloads yet.", { exact: true }),
    ).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath("requests-mobile.png") });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    if (role === "admin") {
      await page.route("**/api/requests?*", (route) =>
        route.fulfill({
          json: {
            total: 1,
            offset: 0,
            limit: 10,
            items: [
              {
                id: "transfer",
                work_id: "work",
                work_title: "The Long Way Home",
                description: "Audiobook edition",
                approval_status: "approved",
                reasons: [],
                specification: { mode: "audio" },
                targets: [
                  {
                    slot: "audio",
                    state: "wanted",
                    message: "Downloading selected release",
                    progress: 0.25,
                    attempt_state: "downloading",
                    attempt_id: "attempt",
                    next_action: "downloads",
                  },
                ],
              },
            ],
          },
        }),
      );
      await page.reload();
      await expect(
        page.getByRole("progressbar", {
          name: "The Long Way Home audiobook download progress",
        }),
      ).toHaveAttribute("value", "0.25");
      await expect(page.getByText("25%", { exact: true })).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath("queue-mobile.png") });
      await page.setViewportSize({ width: 1440, height: 1000 });
      await page.screenshot({ path: testInfo.outputPath("queue-desktop.png") });
    }
    expect(errors).toEqual([]);
  });
}
