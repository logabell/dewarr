import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("Discover opens a full book page with safe reviews and explicit actions", async ({
  page,
}, testInfo) => {
  test.setTimeout(90_000);
  execFileSync("uv", ["run", "python", "scripts/e2e_auth_budget.py"], {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    stdio: "pipe",
  });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
  if (await page.getByLabel("Your name").isVisible()) {
    await page.getByLabel("Your name").fill("Test Reader");
    await page.getByLabel("Username", { exact: true }).fill("reader");
    await page
      .getByLabel("Password", { exact: true })
      .fill("browser test password");
    await page.getByRole("button", { name: "Create administrator" }).click();
    await page
      .getByRole("button", { name: "Finish later", exact: true })
      .click();
    await page.getByRole("link", { name: "My Library", exact: true }).click();
  } else {
    await page.getByLabel("Username", { exact: true }).fill("reader");
    await page
      .getByLabel("Password", { exact: true })
      .fill("browser test password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "Sign out", exact: true }),
    ).toBeVisible();
    await page.goto("/library?view=saved");
  }
  await expect(page.getByRole("heading", { name: "My Library" })).toBeVisible();
  await page.goto("/metadata");
  await page.getByLabel("Hardcover API token").fill("browser-hardcover-token");
  await page
    .locator("section.settings-block")
    .filter({
      has: page.getByRole("heading", { name: "Hardcover", exact: true }),
    })
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(
    page.getByText("Your catalog connection was saved.", { exact: true }),
  ).toBeVisible();
  const auth = await (await page.request.get("/api/auth/me")).json();
  const headers = {
    Origin: "http://127.0.0.1:8001",
    "X-CSRF-Token": auth.csrf_token,
  };
  const prefs = await page.request.get("/api/acquisition/preferences/personal");
  expect(prefs.ok()).toBeTruthy();
  const current = await prefs.json();
  const savedPrefs = await page.request.put(
    "/api/acquisition/preferences/personal",
    {
      headers,
      data: {
        overrides: { ...current.overrides, desired_media: "ebook" },
        expected_revision: current.revision,
      },
    },
  );
  expect(savedPrefs.ok()).toBeTruthy();
  const list = await page.request.post("/api/lists", {
    headers,
    data: { name: "Detail page reading list" },
  });
  expect(list.ok()).toBe(true);
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST") writes.push(request.url());
  });
  // Give this journey its own provider identity when the full suite has
  // already imported the shared discovery fixture.
  await page.route("**/api/discovery/hardcover/trending?*", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({
      response,
      json: {
        ...data,
        items: [
          {
            book: {
              provider: "hardcover",
              external_id: "9010",
              title: "The Discovered Harbor",
              authors: ["Catalog Author"],
            },
            work: null,
            reason: data.attribution,
          },
        ],
      },
    });
  });
  await page.goto("/discover");
  const trending = page.getByRole("region", {
    name: "Trending books",
    exact: true,
  });
  const link = trending.getByRole("link", {
    name: "View The Discovered Harbor",
    exact: true,
  });
  await link.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/discover\/books\/hardcover\/9010$/);
  const title = page.getByRole("heading", {
    name: "The Discovered Harbor",
    level: 1,
  });
  await expect(title).toBeFocused({ timeout: 30_000 });
  await expect(
    page.getByRole("heading", { name: "About this book" }),
  ).toBeVisible();
  await expect(page.getByText("4.25", { exact: true }).first()).toBeVisible();
  await expect(
    page.getByText("The lighthouse keeper returns home."),
  ).not.toBeVisible();
  const overviewTab = page.getByRole("tab", { name: "Overview", exact: true });
  await expect(overviewTab).toHaveAttribute("aria-selected", "true");
  await overviewTab.focus();
  await page.keyboard.press("End");
  await expect(
    page.getByRole("tab", { name: "Reviews", exact: true }),
  ).toBeFocused();
  await expect(
    page.getByRole("heading", { name: "About this book" }),
  ).toHaveCount(0);
  await page.getByText("Contains spoilers · Reveal review").click();
  await expect(
    page.getByText("The lighthouse keeper returns home."),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Authors", exact: true }).click();
  await expect(page.getByText("Read biography", { exact: true })).toHaveCount(
    0,
  );
  await expect(
    page.getByText("A writer of coastal mysteries and distant journeys."),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Editions", exact: true }).click();
  await page.getByLabel("Edition format").selectOption("ebook");
  await expect(page.locator(".reader-edition")).toHaveCount(1);
  expect(writes).toEqual([]);
  await page.getByLabel("Edition format").selectOption("all");
  await overviewTab.click();
  const hero = await page.locator(".reader-hero").boundingBox();
  const tabs = await page
    .getByRole("tablist", { name: "Book sections" })
    .boundingBox();
  expect(tabs!.y - (hero!.y + hero!.height)).toBeLessThanOrEqual(8);
  expect(tabs!.y).toBeLessThan(650);
  await page.screenshot({
    path: testInfo.outputPath("book-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("book-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.route("**/reader-details", (route) =>
    route.fulfill({
      status: 503,
      json: { detail: "Reviews temporarily unavailable" },
    }),
  );
  await page.reload();
  await expect(title).toBeVisible();
  await page.getByRole("tab", { name: "Reviews", exact: true }).click();
  await expect(page.getByText("Reviews temporarily unavailable")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Quick add", exact: true }),
  ).toBeEnabled();
  await page.unroute("**/reader-details");
  await page
    .getByRole("button", { name: "Retry author details and reviews" })
    .click();
  await expect(page.getByText("4.25", { exact: true }).first()).toBeVisible();
  let quickReceipt: unknown = null;
  await page.route("**/api/requests/quick-add/latest/*", (route) =>
    route.fulfill({ json: quickReceipt }),
  );
  await page.route("**/api/requests/quick-add", (route) => {
    quickReceipt = {
      id: "quick-fixture",
      status: "completed",
      message: "Preferred downloads queued",
    };
    return route.fulfill({ status: 202, json: quickReceipt });
  });
  await page.getByRole("button", { name: "Quick add", exact: true }).click();
  await expect(page.getByText("Preferred downloads queued")).toBeVisible();
  expect(writes.filter((url) => url.endsWith("/import"))).toHaveLength(1);
  expect(
    writes.filter((url) => url.endsWith("/api/requests/quick-add")),
  ).toHaveLength(1);
  await page.getByRole("button", { name: "Add to list", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Add to a reading list" }),
  ).toBeVisible();
  await page.getByRole("radio", { name: "Detail page reading list" }).click();
  await page.getByRole("button", { name: "Save to list", exact: true }).click();
  await expect(
    page.getByText("Added to your list.", { exact: true }),
  ).toBeVisible();
  expect(writes.filter((url) => url.endsWith("/import"))).toHaveLength(1);
  await page
    .getByRole("button", { name: "Search sources", exact: true })
    .click();
  await expect(page).toHaveURL(/\/books\/[^?]+\?tab=sources$/);
  expect(errors).toEqual([]);
});
