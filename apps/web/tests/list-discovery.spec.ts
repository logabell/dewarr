import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("followed lists expose saved books and ownership without starting acquisition", async ({
  page,
}, testInfo) => {
  test.setTimeout(60_000);
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
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Sign out", exact: true }),
  ).toBeVisible();
  await page.goto("/library?view=saved");
  await expect(page.getByRole("heading", { name: "My Library" })).toBeVisible();
  const auth = await (await page.request.get("/api/auth/me")).json();
  const headers = {
    "X-CSRF-Token": auth.csrf_token,
    Origin: "http://127.0.0.1:8001",
  };
  async function post(url: string, data: unknown) {
    const response = await page.request.post(url, { headers, data });
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  }
  const holdings = await (
    await page.request.get("/api/discovery/library")
  ).json();
  const owned = holdings.items[0].work;
  const extra = await post("/api/catalog/works", {
    title: "Followed list discovery newcomer",
    authors: ["Discovery writer"],
  });
  const second = await post("/api/catalog/works", {
    title: "Another followed list title",
    authors: [],
  });
  let latest: { id: string; name: string } | null = null;
  for (let i = 0; i < 6; i++) {
    const item = await post("/api/lists", { name: `Followed discovery ${i}` });
    const subscription = await page.request.put(
      `/api/lists/${item.id}/subscription`,
      {
        headers,
        data:
          i === 0
            ? { provider: "hardcover", hardcover_list_id: 9101, enabled: false }
            : {
                feed_url: `https://www.goodreads.com/review/list_rss/123?key=browser-test-only&shelf=discovery-${i}`,
                enabled: false,
              },
      },
    );
    expect(subscription.ok(), await subscription.text()).toBe(true);
    if (i === 5) {
      latest = item;
      for (const work of [owned, extra, second]) {
        const response = await page.request.post(
          `/api/lists/${item.id}/entries`,
          { headers, data: { work_id: work.id } },
        );
        expect(response.ok()).toBe(true);
      }
    }
  }
  expect(latest).not.toBeNull();

  const writes: string[] = [];
  page.on("request", (request) => {
    // Visible cards look up their Hardcover match in one read-only batch.
    const lookup = request.url().endsWith("/api/metadata/reader-matches");
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method()) && !lookup)
      writes.push(request.url());
  });
  await page.goto("/discover?view=yours");
  const shelf = page.getByRole("region", {
    name: "Your followed lists",
    exact: true,
  });
  const card = shelf.getByRole("region", {
    name: `${latest!.name} followed list`,
    exact: true,
  });
  await expect(card).toContainText("3 books");
  await expect(card.locator(".book-card:not(.shelf-view-all)")).toHaveCount(3);
  await expect(
    card.getByRole("img", { name: "In library", exact: true }),
  ).toHaveCount(1);
  await expect(
    card.getByRole("button", { name: `Refresh ${latest!.name}`, exact: true }),
  ).toBeDisabled();
  await shelf.scrollIntoViewIfNeeded();
  if (await shelf.locator(".infinite-scroll").count())
    await shelf.locator(".infinite-scroll").scrollIntoViewIfNeeded();
  await expect
    .poll(() => shelf.locator(":scope > .discovery-section").count())
    .toBeGreaterThanOrEqual(6);
  await page.setViewportSize({ width: 390, height: 844 });
  await card.screenshot({
    path: testInfo.outputPath("followed-lists-mobile.png"),
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await card.getByRole("link", { name: "View all", exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`view=yours&list=${latest!.id}`));
  await expect(
    page.getByRole("heading", { name: latest!.name, exact: true }),
  ).toBeVisible();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});
