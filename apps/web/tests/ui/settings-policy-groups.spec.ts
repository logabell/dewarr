import { expect, test, type Page } from "../fixtures";

async function mockSettings(page: Page, role = "admin") {
  const requests: string[] = [];
  let recovery = {
    attempt_cap: 3,
    approve_reports: true,
    defaults: {
      enabled: true,
      stall_hours: 24,
      error_minutes: 5,
      cleanup: "leave",
    },
    sources: {
      mam: {
        enabled: true,
        stall_hours: null,
        error_minutes: 5,
        cleanup: "leave",
      },
    },
  };
  const policies = new Map<string, unknown>();
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = decodeURIComponent(new URL(request.url()).pathname);
    requests.push(path);
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "admin",
          role,
          display_name: "Administrator",
          onboarding_status: "complete",
          permissions: role === "member" ? ["manage_users"] : [],
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/auth/access") data = { roles: [], permissions: [] };
    else if (path === "/api/auth/oidc/settings") data = { enabled: false };
    else if (path === "/api/auth/plex/settings")
      data = { enabled: false, servers: [] };
    else if (path === "/api/acquisition/recovery/settings") {
      if (request.method() === "PUT") recovery = request.postDataJSON();
      data = recovery;
    } else if (path === "/api/request-quotas") {
      data = Array.from(policies, ([scope, rules]) => ({ scope, rules }));
    } else if (path === "/api/request-quotas/users")
      data = [
        {
          user_id: "reader",
          user_name: "Alex Reader",
          pending: 2,
          bypass: false,
          windows: [
            {
              medium: "ebook",
              window: "week",
              used_books: 2,
              books: 5,
              used_bytes: 1024 ** 3,
            },
          ],
        },
      ];
    else if (
      path.startsWith("/api/request-quotas/") &&
      request.method() === "PUT"
    ) {
      const scope = path.split("/").at(-1)!;
      policies.set(scope, request.postDataJSON());
      data = { scope, rules: request.postDataJSON() };
    } else if (
      path.startsWith("/api/request-quotas/") &&
      request.method() === "DELETE"
    ) {
      policies.delete(path.split("/").at(-1)!);
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({ json: data });
  });
  return { requests, policies, recovery: () => recovery };
}

test("recovery is grouped with clients, keeps collapsed drafts, and saves source selections", async ({
  page,
}, testInfo) => {
  const fixture = await mockSettings(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/settings#downloaders");
  const categories = page.getByRole("navigation", {
    name: "Settings categories",
  });
  await expect(
    categories.getByRole("link", { name: "Download recovery", exact: true }),
  ).toHaveCount(0);
  await expect(
    categories.getByRole("link", { name: "Request quotas", exact: true }),
  ).toHaveCount(0);
  const group = page.locator(".settings-group > summary");
  await expect(group).toContainText("Download recovery");
  expect(fixture.requests).not.toContain("/api/acquisition/recovery/settings");
  await group.focus();
  await page.keyboard.press("Enter");
  const form = page.getByRole("form", { name: "Download recovery settings" });
  await form.getByLabel("Maximum attempts", { exact: false }).fill("4");
  await group.click();
  await expect(form).not.toBeVisible();
  await group.click();
  await expect(
    form.getByLabel("Maximum attempts", { exact: false }),
  ).toHaveValue("4");
  await form
    .getByRole("combobox", { name: "Source", exact: true })
    .selectOption("indexer");
  await expect(
    form.getByRole("button", { name: "Add source override" }),
  ).toBeDisabled();
  await form.getByLabel("Prowlarr indexer ID").fill("12");
  await form.getByRole("button", { name: "Add source override" }).click();
  const indexer = form.getByRole("group", {
    name: "Prowlarr — indexer 12",
    exact: true,
  });
  await indexer.getByLabel("Failed transfer cleanup").selectOption("pause");
  await form.getByRole("button", { name: "Save recovery settings" }).click();
  await expect(page.getByRole("status")).toContainText(
    "Recovery settings saved",
  );
  expect(fixture.recovery()).toMatchObject({
    attempt_cap: 4,
    sources: {
      "prowlarr:12": { cleanup: "pause" },
      mam: { stall_hours: null },
    },
  });
  await page.goto("/settings?keep=yes#recovery");
  await expect(page).toHaveURL(/keep=yes&group=recovery#downloaders$/);
  await expect(form).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("recovery-desktop.png"),
    fullPage: true,
  });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(
    page.locator('.page-tabs a[aria-current="page"]'),
  ).toBeInViewport();
  await page.screenshot({
    path: testInfo.outputPath("recovery-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});

test("quotas live under users, validate limits, save overrides, and restore inheritance", async ({
  page,
}, testInfo) => {
  const fixture = await mockSettings(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/settings#accounts");
  await expect(page.locator(".settings-group > summary")).toContainText(
    "Request quotas",
  );
  expect(fixture.requests).not.toContain("/api/request-quotas");
  await page.goto("/settings#quotas");
  await expect(page).toHaveURL(/group=quotas#accounts$/);
  const form = page.getByRole("form", { name: "Request quota settings" });
  await form.getByLabel("Apply limits to").selectOption("user:reader");
  await form.getByRole("button", { name: "Add rolling limit" }).click();
  await form.getByLabel("Book limit", { exact: true }).fill("5");
  await form.getByLabel("Size limit (GiB)").fill("2");
  await form.getByLabel("Maximum pending approvals").fill("3");
  await form.getByRole("button", { name: "Save limits", exact: true }).click();
  await expect(form.getByRole("status")).toHaveText("Request limits saved.");
  expect(fixture.policies.get("user:reader")).toMatchObject({
    windows: [
      {
        medium: "combined",
        window: "week",
        books: 5,
        size_bytes: 2 * 1024 ** 3,
      },
    ],
    pending_cap: 3,
  });
  await form.getByRole("button", { name: "Add rolling limit" }).click();
  await expect(form.getByRole("status")).toHaveCount(0);
  await form
    .getByRole("combobox", { name: "Window", exact: true })
    .nth(1)
    .selectOption("week");
  await expect(form.getByRole("alert")).toContainText("only once");
  await expect(
    form.getByRole("button", { name: "Save limits", exact: true }),
  ).toBeDisabled();
  await form
    .getByRole("combobox", { name: "Window", exact: true })
    .nth(1)
    .selectOption("month");
  await page.getByText("Per-user usage", { exact: true }).click();
  await expect(
    page.getByRole("table", { name: "Per-user request usage" }),
  ).toContainText("Alex Reader");
  await page.screenshot({
    path: testInfo.outputPath("quotas-desktop.png"),
    fullPage: true,
  });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(
    page.locator('.page-tabs a[aria-current="page"]'),
  ).toBeInViewport();
  await page.screenshot({
    path: testInfo.outputPath("quotas-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await form.getByRole("button", { name: "Restore inherited limits" }).click();
  await expect(form.getByRole("status")).toHaveText(
    "Inherited limits restored.",
  );
  expect(fixture.policies.has("user:reader")).toBe(false);
  expect(errors).toEqual([]);
});

test("user managers cannot open administrator quota controls", async ({
  page,
}) => {
  const fixture = await mockSettings(page, "member");
  await page.goto("/settings#quotas");
  await expect(
    page.getByRole("region", { name: "Users & access", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".settings-group")).toHaveCount(0);
  expect(fixture.requests).not.toContain("/api/request-quotas");
});
