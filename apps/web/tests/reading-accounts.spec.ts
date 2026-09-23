import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("connect Goodreads in onboarding, choose shelves, pause and refresh from settings", async ({
  page,
}, testInfo) => {
  execFileSync("uv", ["run", "python", "scripts/e2e_auth_budget.py"], {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    stdio: "pipe",
  });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page.getByLabel("Username", { exact: true }).fill("reader");
  await page
    .getByLabel("Password", { exact: true })
    .fill("browser test password");
  if (await page.getByLabel("Your name").isVisible()) {
    await page.getByLabel("Your name").fill("Test Reader");
    await page.getByRole("button", { name: "Create administrator" }).click();
    await page
      .getByRole("button", { name: "Finish later", exact: true })
      .click();
  } else {
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  }
  await expect(
    page.getByRole("button", { name: /Finish later|Sign out/ }),
  ).toBeVisible();
  const auth = await (await page.request.get("/api/auth/me")).json();
  await page.request.put("/api/metadata/account", {
    headers: {
      Origin: "http://127.0.0.1:8001",
      "X-CSRF-Token": auth.csrf_token,
    },
    data: { token: "browser-hardcover-token", enabled: true },
  });
  await page.request.put("/api/setup/onboarding", {
    headers: {
      Origin: "http://127.0.0.1:8001",
      "X-CSRF-Token": auth.csrf_token,
    },
    data: { status: "pending", step: 5, skipped: [] },
  });
  const connection = {
    user_id: "123",
    name: "Test reader",
    profile_url: "https://www.goodreads.com/user/show/123",
    selected: null,
    shelves: [
      { external_id: "to-read", name: "Want to read", count: 33 },
      { external_id: "fantasy", name: "Fantasy", count: 1 },
    ],
    discovered_at: new Date().toISOString(),
    warning: null,
  };
  let connected = false;
  const tracked: any[] = [];
  let updates = 0;
  await page.route("**/api/reading-accounts/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/goodreads")) {
      if (route.request().method() === "PUT") {
        expect(route.request().postDataJSON().profile).toBe(
          "https://www.goodreads.com/user/show/123-reader",
        );
        connected = true;
      }
      await route.fulfill({ json: connected ? connection : null });
    } else if (path.endsWith("/subscriptions")) {
      await route.fulfill({ json: tracked });
    } else if (path.endsWith("/follow")) {
      const body = route.request().postDataJSON();
      expect(body.interval_minutes).toBe(60);
      const choice = connection.shelves.find(
        (s) => s.external_id === body.external_id,
      )!;
      tracked.push({
        list_id: "00000000-0000-0000-0000-000000000001",
        name: choice.name,
        external_id: choice.external_id,
        account_id: "123",
        subscription: {
          id: "00000000-0000-0000-0000-000000000002",
          provider: "goodreads",
          enabled: true,
          interval_minutes: 60,
          state: "idle",
          generation: 1,
          observed_count: 33,
          last_success_at: new Date().toISOString(),
          next_sync_at: new Date(Date.now() + 3600000).toISOString(),
        },
      });
      await route.fulfill({
        json: { list_id: tracked[0].list_id, reused: false },
      });
    } else if (path.endsWith("/discover")) {
      await route.fulfill({ json: connection });
    } else await route.continue();
  });
  await page.route("**/api/lists/*/subscription**", async (route) => {
    if (route.request().url().endsWith("/sync")) {
      updates++;
      await route.fulfill({ json: { id: "op", status: "queued" } });
    } else {
      const body = route.request().postDataJSON();
      expect(body.expected_generation).toBe(tracked[0].subscription.generation);
      Object.assign(tracked[0].subscription, body, {
        generation: body.expected_generation + 1,
      });
      await route.fulfill({ json: tracked[0].subscription });
    }
  });
  await page.reload();
  await expect(page).toHaveURL(/\/onboarding$/);
  await expect(
    page.getByRole("heading", { name: "Reading accounts", exact: true }),
  ).toBeVisible();
  const goodreads = page.getByRole("region", { name: "Goodreads connection" });
  await goodreads.locator(".reading-connection > summary").click();
  await expect(
    goodreads.getByRole("link", { name: /Open my Goodreads books/ }),
  ).toHaveAttribute("href", "https://www.goodreads.com/review/list");
  await goodreads
    .getByLabel("Goodreads profile or books link")
    .fill("https://www.goodreads.com/user/show/123-reader");
  await goodreads
    .getByRole("button", { name: "Find my Goodreads shelves" })
    .click();
  await expect(
    goodreads.getByRole("checkbox", { name: /Want to read/ }),
  ).toBeChecked();
  await goodreads.getByRole("button", { name: "Track selected lists" }).click();
  const lists = goodreads;
  await expect(
    lists.getByRole("link", { name: "Want to read", exact: true }),
  ).toBeVisible();
  await lists
    .getByRole("button", { name: "Pause tracking Want to read" })
    .click();
  await expect(
    lists.getByRole("button", { name: "Resume tracking Want to read" }),
  ).toBeVisible();
  await expect(
    lists.getByRole("button", { name: "Check for updates to Want to read" }),
  ).toBeDisabled();
  await expect(lists).toContainText("Paused");
  await lists
    .getByRole("button", { name: "Resume tracking Want to read" })
    .click();
  await expect(
    lists.getByRole("button", { name: "Pause tracking Want to read" }),
  ).toBeVisible();
  await lists
    .getByRole("button", { name: "Check for updates to Want to read" })
    .click();
  await expect.poll(() => updates).toBe(1);
  await page.getByRole("button", { name: "Next step" }).click();
  await page.getByRole("button", { name: "Start browsing" }).click();
  await page.goto("/settings#reading");
  await goodreads.locator(".reading-connection > summary").click();
  await expect(
    lists.getByRole("button", { name: "Pause tracking Want to read" }),
  ).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "Settings sections" }),
  ).toHaveCount(0);
  await expect(
    page
      .getByRole("navigation", { name: "Settings categories" })
      .getByRole("link", { name: "Reading accounts", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(
    lists.getByRole("link", { name: "Want to read", exact: true }),
  ).toHaveCount(1);
  await page.locator("#reading").screenshot({
    path: testInfo.outputPath("reading-accounts-desktop.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator("#reading").scrollIntoViewIfNeeded();
  await page.locator("#reading").screenshot({
    path: testInfo.outputPath("reading-accounts-mobile.png"),
  });
  const overflow = await page.locator("body *").evaluateAll((nodes) =>
    nodes
      .filter(
        (node) =>
          node.getBoundingClientRect().right > innerWidth + 1 &&
          getComputedStyle(node).display !== "none",
      )
      .map((node) => ({
        tag: node.tagName,
        class: node.className,
        right: node.getBoundingClientRect().right,
        text: node.textContent?.slice(0, 80),
      })),
  );
  if (
    await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
  )
    console.log("Overflow", JSON.stringify(overflow));
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});

test("tracked lists remain manageable when account discovery fails", async ({
  page,
}) => {
  let attempts = 0;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "member",
          display_name: "Reader",
          onboarding_status: "complete",
          permissions: [],
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/lists/page")
      data = { items: [], total: 0, offset: 0, limit: 25 };
    else if (path === "/api/reading-accounts/goodreads") data = null;
    else if (path === "/api/reading-accounts/storygraph") data = null;
    else if (path === "/api/metadata/account") data = { enabled: true };
    else if (path === "/api/reading-accounts/subscriptions")
      data = [
        {
          list_id: "hardcover-list",
          name: "Weekend reading",
          external_id: "123",
          account_id: null,
          subscription: {
            provider: "hardcover",
            enabled: true,
            interval_minutes: 60,
            generation: 1,
            state: "failed",
            message: "Connection needs attention",
            observed_count: 12,
          },
        },
      ];
    else if (path === "/api/metadata/hardcover-lists") {
      attempts++;
      if (attempts === 1)
        return route.fulfill({
          status: 503,
          json: { detail: "Lists temporarily unavailable" },
        });
      data = {
        items: [{ external_id: "123", name: "Weekend reading", count: 12 }],
        next_cursor: null,
      };
    }
    if (
      new URL(route.request().url()).pathname.includes(
        "/acquisition/preferences/",
      )
    )
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#reading");
  const hardcover = page.getByRole("region", { name: "Hardcover connection" });
  await hardcover.locator(".reading-connection > summary").click();
  await expect(
    hardcover.getByText("Needs attention", { exact: true }),
  ).toBeVisible();
  await expect(
    hardcover.getByRole("button", { name: "Pause tracking Weekend reading" }),
  ).toBeEnabled();
  await hardcover
    .getByRole("button", { name: "Refresh lists", exact: true })
    .click();
  await expect.poll(() => attempts).toBe(2);
  await expect(
    hardcover.getByRole("link", { name: "Weekend reading", exact: true }),
  ).toHaveCount(1);
  await expect(hardcover.getByText("12 books", { exact: true })).toHaveCount(1);
});

test("connect StoryGraph, follow a shelf, and paste a tag list", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const shelves = [
    { external_id: "to-read", name: "To-read", count: 2, kind: "shelf" },
    {
      external_id: "9d7e824e-e4d8-40b1-8fef-6d6b5a6a44ba",
      name: "Summer",
      count: null,
      kind: "tag",
    },
  ];
  const account = {
    username: "nadia",
    profile_url: "https://app.thestorygraph.com/profile/nadia",
    shelves,
    discovered_at: new Date().toISOString(),
  };
  let connected = false;
  const tracked: any[] = [];
  let pasted = false;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "member",
          display_name: "Reader",
          onboarding_status: "complete",
          permissions: [],
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/lists/page")
      data = { items: [], total: 0, offset: 0, limit: 25 };
    else if (path === "/api/reading-accounts/goodreads") data = null;
    else if (path === "/api/reading-accounts/storygraph") {
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON();
        expect(body.session_cookie).toBe("session-token");
        expect(body.remember_token).toBe("remember-token");
        connected = true;
      }
      data = connected ? account : null;
    } else if (path === "/api/metadata/account") data = { enabled: true };
    else if (path === "/api/reading-accounts/subscriptions") data = tracked;
    else if (path === "/api/reading-accounts/follow") {
      const body = route.request().postDataJSON();
      expect(body.provider).toBe("storygraph");
      expect(body.external_id).toBe("to-read");
      tracked.push({
        list_id: "00000000-0000-0000-0000-000000000031",
        name: "To-read",
        external_id: "to-read",
        account_id: "nadia",
        subscription: {
          id: "00000000-0000-0000-0000-000000000032",
          provider: "storygraph",
          enabled: true,
          interval_minutes: 60,
          generation: 1,
          state: "idle",
          message: "Waiting for the next shelf observation",
          observed_count: 0,
          shelf: "To-read",
        },
      });
      data = { list_id: tracked[0].list_id, reused: false };
    } else if (path === "/api/discovery/layout")
      data = { hidden: [], order: [] };
    else if (path === "/api/discovery/collections")
      data = {
        items: [],
        total: 0,
        years: [],
        genres: [],
        categories: [],
        archive_gaps: [],
      };
    else if (path === "/api/discovery/personal-list/preview") {
      expect(route.request().postDataJSON().url).toBe(
        "https://app.thestorygraph.com/tags/9d7e824e-e4d8-40b1-8fef-6d6b5a6a44ba",
      );
      data = { name: "Summer", count: 1, titles: ["Harbor"], list_id: null };
    } else if (path === "/api/discovery/personal-list") {
      pasted = true;
      data = {
        name: "Summer",
        count: 1,
        titles: [],
        list_id: "00000000-0000-0000-0000-000000000033",
      };
    }
    if (path.includes("/acquisition/preferences/"))
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/settings#reading");
  const storygraph = page.getByRole("region", {
    name: "StoryGraph connection",
  });
  await storygraph.locator(".reading-connection > summary").click();
  const guide = storygraph.locator(".storygraph-setup");
  await expect(guide).toBeVisible();
  await expect(guide.locator("ol")).toBeHidden();
  await guide.locator("summary").click();
  const login = guide.getByRole("link", { name: "Sign in to StoryGraph ↗" });
  await expect(login).toHaveAttribute(
    "href",
    "https://app.thestorygraph.com/users/sign_in",
  );
  await expect(login).toHaveAttribute("target", "_blank");
  await expect(guide).toContainText("_storygraph_session");
  await expect(guide).toContainText("remember_user_token");
  await storygraph.getByLabel("_storygraph_session").fill("session-token");
  await storygraph.getByLabel("remember_user_token").fill("remember-token");
  await storygraph
    .getByRole("button", { name: "Find my StoryGraph lists" })
    .click();
  await expect(
    storygraph.getByRole("checkbox", { name: /To-read/ }),
  ).toBeChecked();
  await storygraph
    .getByRole("button", { name: "Track selected lists" })
    .click();
  await expect(
    storygraph.getByRole("link", { name: "To-read", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Add list" }).click();
  const dialog = page.getByRole("dialog", { name: "Add a list" });
  await dialog
    .getByLabel("List URL")
    .fill(
      "https://app.thestorygraph.com/tags/9d7e824e-e4d8-40b1-8fef-6d6b5a6a44ba",
    );
  await dialog.getByRole("button", { name: "Preview" }).click();
  await expect(dialog.getByRole("heading", { name: "Summer" })).toBeVisible();
  await expect(dialog).toContainText("1 book on this list");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 390, height: 844 });
  await dialog.screenshot({
    path: testInfo.outputPath("storygraph-add-list-narrow.png"),
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await dialog.getByRole("button", { name: "Add list" }).click();
  await expect(page).toHaveURL(/\/discover\?view=yours/);
  await expect.poll(() => pasted).toBe(true);
  await page.goto("/settings#reading");
  await storygraph.locator(".reading-connection > summary").click();
  await expect(
    storygraph.getByRole("link", { name: "To-read", exact: true }),
  ).toBeVisible();
  await page.locator("#reading").screenshot({
    path: testInfo.outputPath("storygraph-reading-narrow.png"),
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});
