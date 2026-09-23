import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("operation history filters real curation records and preserves navigation state", async ({
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
    title: "Operation history book",
    authors: ["Activity Author"],
  });
  const list = await post("/api/lists", {
    name: "Operation history reading list",
  });
  let receipt: { id: string } = { id: "" };
  for (let i = 0; i < 60; i++) {
    receipt = await post(`/api/lists/${list.id}/curation`, {
      action: i % 2 ? "remove" : "add",
      work_ids: [work.id],
    });
  }
  const expected = await (
    await page.request.get("/api/activity/page?kind=lists.curate&limit=100")
  ).json();
  expect(expected.total).toBeGreaterThanOrEqual(60);
  const writes: string[] = [];
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method()))
      writes.push(request.url());
  });
  await page.goto("/settings#logs");
  const history = page.getByRole("region", {
    name: "Background activity",
    exact: true,
  });
  await history
    .getByRole("combobox", { name: "Task type" })
    .selectOption("lists.curate");
  await expect(history.locator(".logs-entry")).toHaveCount(50);
  await expect(history.getByRole("status")).toHaveText(
    `${expected.total} matching operations`,
  );
  await history.locator(".logs-entry").last().scrollIntoViewIfNeeded();
  await expect(history.locator(".logs-entry")).toHaveCount(50);
  await expect(
    history.getByRole("button", { name: "Previous", exact: true }),
  ).toBeDisabled();
  await history.getByRole("button", { name: "Next", exact: true }).click();
  await expect(page).toHaveURL(/offset=50/);
  await expect(history.locator(".logs-entry")).toHaveCount(
    Math.min(50, expected.total - 50),
  );
  await page.reload();
  await expect(history.getByText("Page 2 · 50 per page")).toBeVisible();
  await history.getByRole("button", { name: "Previous", exact: true }).click();
  await expect(history.locator(".logs-entry")).toHaveCount(50);
  await history.getByRole("button", { name: "Next", exact: true }).click();
  await history
    .getByRole("combobox", { name: "Activity status" })
    .selectOption("completed");
  await expect(page).not.toHaveURL(/offset=/);
  await history
    .getByRole("searchbox", { name: "Search activity", exact: true })
    .fill(receipt.id);
  await history
    .getByRole("button", { name: "Search activity", exact: true })
    .click();
  await expect(history.locator(".logs-entry")).toHaveCount(1);
  await expect(history.getByRole("status")).toHaveText("1 matching operation");
  await history
    .getByRole("button", {
      name: "Operation details: List curation",
      exact: true,
    })
    .click();
  await expect(
    history.getByText(`Operation ID: ${receipt.id}`, { exact: true }),
  ).toBeVisible();
  await history.screenshot({
    path: testInfo.outputPath("operation-history-desktop.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await history.screenshot({
    path: testInfo.outputPath("operation-history-mobile.png"),
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const context = history.getByRole("link", {
    name: "Open list: Operation history reading list",
    exact: true,
  });
  await context.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(`/discover?view=yours&list=${list.id}`);
  await expect(
    page.getByRole("heading", { name: "Operation history reading list" }),
  ).toBeVisible();
  await page.goBack();
  await expect(
    history.getByRole("searchbox", { name: "Search activity", exact: true }),
  ).toHaveValue(receipt.id);
  await expect(history.locator(".logs-entry")).toHaveCount(1);
  await page.route("**/api/activity/page?*", (route) =>
    route.fulfill({
      status: 503,
      json: { detail: "Synthetic operation history outage" },
    }),
  );
  await expect(history.getByRole("alert")).toHaveText(
    "Synthetic operation history outage",
    { timeout: 20_000 },
  );
  await expect(history.locator(".logs-entry")).toHaveCount(0);
  await page.unroute("**/api/activity/page?*");
  await history
    .getByRole("button", { name: "Retry activity", exact: true })
    .click();
  await expect(context).toBeVisible();
  await history
    .getByRole("searchbox", { name: "Search activity", exact: true })
    .fill("missing_%_history");
  await history
    .getByRole("button", { name: "Search activity", exact: true })
    .click();
  await expect(
    history.getByRole("heading", { name: "No matching activity", exact: true }),
  ).toBeVisible();
  await history.getByRole("button", { name: "Reset activity view" }).click();
  await expect(
    history.getByRole("searchbox", { name: "Search activity", exact: true }),
  ).toHaveValue("");
  await expect(
    history.getByRole("combobox", { name: "Task type" }),
  ).toHaveValue("");
  await expect(history.locator(".logs-entry")).toHaveCount(50);
  expect(errors).toEqual([]);
  expect(writes).toEqual([]);
});
