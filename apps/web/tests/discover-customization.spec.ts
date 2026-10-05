import { expect, test } from "./fixtures";

test.beforeEach(async ({ page }) => {
  const bootstrap = await page.request.post("/api/auth/bootstrap", {
    headers: { Origin: "http://127.0.0.1:8001" },
    data: {
      username: "reader",
      display_name: "Reader",
      password: "browser test password",
    },
  });
  const login =
    bootstrap.status() === 201
      ? bootstrap
      : await page.request.post("/api/auth/login", {
          headers: { Origin: "http://127.0.0.1:8001" },
          data: { username: "reader", password: "browser test password" },
        });
  expect(login.ok()).toBeTruthy();
  const auth = await login.json();
  await page.request.put("/api/setup/onboarding", {
    headers: {
      Origin: "http://127.0.0.1:8001",
      "X-CSRF-Token": auth.csrf_token,
    },
    data: { status: "completed", step: 0, skipped: [] },
  });
});

test("customizer aligns controls, reorders, adds shelves and saves only on request", async ({
  page,
}, info) => {
  let saved: { order: string[]; hidden: string[] } | undefined;
  await page.route("**/api/discovery/home", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: {
        ...(await response.json()),
        layout: saved || { order: [], hidden: [] },
      },
    });
  });
  await page.route("**/api/discovery/layout", (route) =>
    route.fulfill({ json: { order: [], hidden: [] } }),
  );
  await page.route("**/api/discovery/collections/*/follow", (route) =>
    route.fulfill({ json: {} }),
  );
  await page.goto("/discover");
  await page.getByRole("button", { name: "Customize", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Customize Discover" });
  const rows = dialog.locator(".sortable > li");
  const firstName = await rows.first().locator("strong").innerText();
  const secondName = await rows.nth(1).locator("strong").innerText();
  await rows
    .first()
    .getByRole("button", { name: `Move ${firstName} down` })
    .click();
  await expect(rows.nth(1).locator("strong")).toHaveText(firstName);
  await rows.nth(1).locator(".drag-handle").focus();
  await page.keyboard.press("ArrowUp");
  await expect(rows.first().locator("strong")).toHaveText(firstName);
  const firstBox = (await rows.first().locator("strong").boundingBox())!;
  const secondBox = (await rows.nth(1).boundingBox())!;
  await page.mouse.move(firstBox.x + 30, firstBox.y + firstBox.height / 2);
  await page.mouse.down();
  await page.mouse.move(
    firstBox.x + 30,
    secondBox.y + secondBox.height / 2 + 8,
    { steps: 12 },
  );
  // The order must change while the pointer is still held down.
  await expect(rows.first().locator("strong")).toHaveText(secondName);
  await expect(rows.nth(1)).toHaveClass(/is-dragging/);
  await page.mouse.move(firstBox.x + 30, firstBox.y, { steps: 12 });
  await expect(rows.first().locator("strong")).toHaveText(firstName);
  await page.mouse.move(
    firstBox.x + 30,
    secondBox.y + secondBox.height / 2 + 8,
    { steps: 12 },
  );
  await expect(rows.first().locator("strong")).toHaveText(secondName);
  await page.mouse.up();
  await expect(dialog.locator(".is-dragging")).toHaveCount(0);
  const toolbar = dialog.locator(".customize-toolbar");
  await expect(
    toolbar.getByRole("button", { name: "Show all shelves" }),
  ).toBeVisible();
  await expect(
    toolbar.getByRole("button", { name: "Add shelves", exact: true }),
  ).toBeVisible();
  const fieldWidth = (await dialog.locator(".customize-fields").boundingBox())!
    .width;
  expect(
    Math.abs((await rows.first().boundingBox())!.width - fieldWidth),
  ).toBeLessThan(2);
  await dialog
    .getByRole("checkbox", { name: firstName, exact: true })
    .uncheck();
  await dialog
    .getByRole("button", { name: "Add shelves", exact: true })
    .click();
  await dialog
    .getByRole("textbox", { name: "Search available shelves" })
    .fill(firstName);
  await dialog
    .getByRole("button", { name: `Add ${firstName}`, exact: true })
    .click();
  await expect(
    dialog.getByRole("checkbox", { name: firstName, exact: true }),
  ).toBeChecked();
  await dialog
    .getByRole("textbox", { name: "Search available shelves" })
    .fill("");
  const candidate = dialog.locator(".customize-options li").first();
  const addedName = await candidate.locator("strong").innerText();
  await candidate.getByRole("button").click();
  await expect(rows.last().locator("strong")).toHaveText(addedName);
  await dialog
    .getByRole("button", { name: "Add shelves", exact: true })
    .click();
  for (const row of await rows.all()) {
    const label = await row.locator("strong").boundingBox();
    const check = await row.getByRole("checkbox").boundingBox();
    expect(check!.x).toBeGreaterThan(label!.x + label!.width);
    expect(
      Math.abs(
        check!.y +
          check!.height / 2 -
          (await row.boundingBox())!.y -
          (await row.boundingBox())!.height / 2,
      ),
    ).toBeLessThan(3);
  }
  await page.screenshot({ path: info.outputPath("customizer-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(
    true,
  );
  await page.screenshot({ path: info.outputPath("customizer-mobile.png") });
  await page.unroute("**/api/discovery/layout");
  await page.route("**/api/discovery/layout", (route) => {
    if (route.request().method() === "PUT")
      saved = route.request().postDataJSON();
    return route.fulfill({ json: saved || { order: [], hidden: [] } });
  });
  await dialog
    .getByRole("button", { name: "Save layout", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(saved!.order.length).toBeGreaterThan(2);
  await page.reload();
  await page.getByRole("button", { name: "Customize", exact: true }).click();
  await expect(
    dialog.locator(".sortable > li").last().locator("strong"),
  ).toHaveText(addedName);
  await dialog
    .getByRole("checkbox", { name: firstName, exact: true })
    .uncheck();
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Customize", exact: true }).click();
  await expect(
    dialog.getByRole("checkbox", { name: firstName, exact: true }),
  ).toBeChecked();
});
