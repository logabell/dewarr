import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("activity requests preserve independent reasons and route missing media to the saved request", async ({
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
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeVisible();
  const auth = await (await page.request.get("/api/auth/me")).json();
  const headers = {
    "X-CSRF-Token": auth.csrf_token,
    Origin: "http://127.0.0.1:8001",
  };
  async function post(url: string, data: unknown) {
    const response = await page.request.post(url, {
      headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
      data,
    });
    expect(response.ok(), await response.text()).toBe(true);
    return response.json();
  }
  const work = await post("/api/catalog/works", {
    title: "Activity Journey Alpha",
    authors: ["Activity Author"],
  });
  const saved = await post("/api/requests", {
    work_id: work.id,
    specification: { mode: "audio" },
  });
  const list = await post("/api/lists", { name: "Activity follow list" });
  // Entry creation returns a normal list response and does not activate automation.
  const entry = await page.request.post(`/api/lists/${list.id}/entries`, {
    headers,
    data: { work_id: work.id },
  });
  expect(entry.ok()).toBe(true);
  const listed = await post("/api/requests", {
    work_id: work.id,
    specification: { mode: "audio" },
    reason: { list_id: list.id },
  });
  expect(listed.request.id).toBe(saved.request.id);
  for (let i = 0; i < 10; i++) {
    const extra = await post("/api/catalog/works", {
      title: `Activity pagination ${i}`,
      authors: [],
    });
    await post("/api/requests", {
      work_id: extra.id,
      specification: { mode: "ebook" },
    });
  }
  const transfers = await (
    await page.request.get("/api/acquisition/downloads")
  ).json();
  const inventory = await (
    await page.request.get("/api/library/assets")
  ).json();
  const writes: string[] = [];
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method()))
      writes.push(request.url());
  });
  await page.getByRole("link", { name: "Requests", exact: true }).click();
  const requests = page.getByRole("region", {
    name: "Requests",
    exact: true,
  });
  await expect
    .poll(() => requests.getByRole("article").count())
    .toBeGreaterThanOrEqual(10);
  await requests.getByRole("article").last().scrollIntoViewIfNeeded();
  const card = requests.getByRole("article", {
    name: "Activity Journey Alpha request",
    exact: true,
  });
  await expect(card).toBeVisible();
  await expect(card).toContainText("Audiobook · Wanted");
  await card.getByText("Details", { exact: true }).click();
  await expect(card).toContainText("Effective request scope");
  await card.getByText("Details", { exact: true }).click();
  expect(writes).toEqual([]);
  await card.getByRole("link", { name: "Choose release", exact: true }).click();
  await expect(page).toHaveURL(
    new RegExp(`request=${saved.request.id}&slot=audio`),
  );
  await expect(
    page.getByRole("region", { name: "Book download sources" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Requests", exact: true }).click();
  await requests.getByRole("article").last().scrollIntoViewIfNeeded();
  await expect(card).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("request-activity-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("request-activity-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await card
    .getByRole("button", { name: "Withdraw your request", exact: true })
    .click();
  await expect(
    card.getByText("Your request · Withdrawn", { exact: true }),
  ).toBeVisible();
  await expect(
    card.getByRole("link", { name: "Choose release", exact: true }),
  ).toBeVisible();
  await card
    .getByRole("button", { name: "Withdraw activity follow list", exact: true })
    .click();
  await expect(card).toHaveCount(0);
  await page.getByRole("button", { name: "Withdrawn", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Withdrawn", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await requests.getByRole("article").last().scrollIntoViewIfNeeded();
  await expect(card).toContainText("Audiobook · Withdrawn");
  await expect(
    card.getByRole("link", { name: "Choose release", exact: true }),
  ).toHaveCount(0);
  await page.route("**/api/requests?*", (route) =>
    route.fulfill({
      status: 503,
      json: { detail: "Synthetic request activity outage" },
    }),
  );
  await expect(
    requests.getByText("Synthetic request activity outage", { exact: true }),
  ).toBeVisible({ timeout: 20_000 });
  // A failed refresh retains the last successful request list.
  await expect(card).toBeVisible();
  await page.unroute("**/api/requests?*");
  await requests.getByRole("button", { name: "Retry requests" }).click();
  await expect(card).toBeVisible();
  const after = await (
    await page.request.get(`/api/requests/${saved.request.id}`)
  ).json();
  expect(
    after.reasons.every((reason: { active: boolean }) => !reason.active),
  ).toBe(true);
  expect(
    (await (await page.request.get("/api/acquisition/downloads")).json()).total,
  ).toBe(transfers.total);
  expect(
    (await (await page.request.get("/api/library/assets")).json()).total,
  ).toBe(inventory.total);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const filters = page.getByRole("navigation", { name: "Request filters" });
  const filterPositions = await filters
    .getByRole("button")
    .evaluateAll((buttons) =>
      buttons.map((button) => button.getBoundingClientRect().top),
    );
  expect(new Set(filterPositions).size).toBe(1);
  await filters
    .getByRole("button", { name: "Downloading", exact: true })
    .click();
  await expect(
    filters.getByRole("button", { name: "Downloading", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await filters.getByRole("button", { name: "Review", exact: true }).click();
  await expect(
    filters.getByRole("button", { name: "Review", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    page.getByRole("region", { name: "Requests", exact: true }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});
