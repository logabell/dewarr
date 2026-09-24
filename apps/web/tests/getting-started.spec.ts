import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("onboarding defers, resumes and completes once; settings show one focused section", async ({
  page,
}, testInfo) => {
  test.setTimeout(120_000);
  execFileSync("uv", ["run", "python", "scripts/e2e_auth_budget.py"], {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    stdio: "pipe",
  });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
  const bootstrap = await page.getByLabel("Your name").isVisible();
  await page.getByLabel("Username", { exact: true }).fill("reader");
  await page
    .getByLabel("Password", { exact: true })
    .fill("browser test password");
  if (bootstrap) {
    await page.getByLabel("Your name").fill("Test Reader");
    await page.getByRole("button", { name: "Create administrator" }).click();
  } else {
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(
      page.getByRole("navigation", { name: "Main navigation" }),
    ).toBeVisible();
    const auth = await (await page.request.get("/api/auth/me")).json();
    await page.request.put("/api/setup/onboarding", {
      headers: {
        Origin: "http://127.0.0.1:8001",
        "X-CSRF-Token": auth.csrf_token,
      },
      data: { status: "pending", step: 0, skipped: [] },
    });
    await page.reload();
  }
  await expect(page).toHaveURL(/\/onboarding$/);
  await expect(
    page.getByRole("heading", { name: "Let’s set up your library" }),
  ).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "Skip this step", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Libraries", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("onboarding-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("onboarding-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "Finish later", exact: true }).click();
  await expect(page).toHaveURL(/\/discover$/);
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Discover", exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await page
    .getByRole("button", { name: "Open onboarding setup wizard" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Libraries", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Finish later", exact: true }).click();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  const navigation = page.getByRole("navigation", { name: "Main navigation" });
  for (const label of [
    "Metadata",
    "Connections",
    "Organization",
    "Accounts",
    "Getting started",
  ])
    await expect(
      navigation.getByRole("link", { name: label, exact: true }),
    ).toHaveCount(0);
  await page.goto("/settings#catalog");
  const catalog = page.getByRole("region", {
    name: "Metadata",
    exact: true,
  });
  await catalog
    .getByLabel("Hardcover API token")
    .fill("browser-hardcover-token");
  await catalog
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(
    catalog.getByText("Your catalog connection was saved."),
  ).toBeVisible();
  await catalog
    .getByRole("button", { name: "Test connection", exact: true })
    .click();
  await expect(
    catalog.getByText("Hardcover catalog access verified."),
  ).toBeVisible();
  await catalog.getByLabel("Primary language").selectOption("fr");
  await catalog.getByRole("button", { name: "Save metadata defaults" }).click();
  await expect(catalog.getByText("Metadata defaults saved.")).toBeVisible();
  await page.goto("/settings#sources");
  const mam = page.getByRole("form", { name: "MAM connection settings" });
  if (!(await mam.isVisible()))
    await page
      .getByRole("region", { name: "MAM settings", exact: true })
      .locator("summary")
      .first()
      .click();
  if (!(await mam.isVisible()))
    await page
      .getByRole("region", { name: "MAM settings", exact: true })
      .locator("summary")
      .first()
      .click();
  await mam.getByText("Advanced settings", { exact: true }).click();
  await mam
    .getByLabel("MAM URL", { exact: true })
    .fill("http://127.0.0.1:13379/mam");
  const mamSession = await (
    await page.request.get("http://127.0.0.1:13379/fixture/mam-session")
  ).json();
  await mam.getByLabel(/^(mam_id|mam_id)$/).fill(mamSession.cookie);
  await mam
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect(mam.getByLabel(/^(mam_id|mam_id)$/)).toHaveValue("");
  await mam.getByRole("button", { name: "Test mam_id", exact: true }).click();
  await expect(
    mam.getByRole("status", { name: "Connection test status" }),
  ).toContainText("Authenticated");
  await page.goto("/settings#naming");
  await expect(page).toHaveURL(/\/settings#naming$/);
  await expect(
    page
      .getByRole("navigation", { name: "Settings categories" })
      .getByRole("link", { name: "File naming", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(catalog).toHaveCount(0);
  await page.goto("/metadata");
  await expect(page).toHaveURL(/\/settings#catalog$/);
  await expect(catalog.getByLabel("Primary language")).toHaveValue("fr");
  await page.screenshot({ path: testInfo.outputPath("settings-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath("settings-mobile.png") });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.goto("/settings#display");
  await page
    .getByRole("button", { name: "Open onboarding setup wizard" })
    .click();
  await page
    .getByRole("navigation", { name: "Setup steps" })
    .getByRole("button", { name: /Ready to go/ })
    .click();
  await page
    .getByRole("button", { name: "Start browsing", exact: true })
    .click();
  await expect(page).toHaveURL(/\/discover$/);
  expect(
    (await (await page.request.get("/api/setup/onboarding")).json()).status,
  ).toBe("completed");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
  await page.getByLabel("Username", { exact: true }).fill("reader");
  await page
    .getByLabel("Password", { exact: true })
    .fill("browser test password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeVisible();
  await expect(page).not.toHaveURL(/\/onboarding$/);
  const auth = await (await page.request.get("/api/auth/me")).json();
  const created = await page.request.post("/api/auth/users", {
    headers: {
      Origin: "http://127.0.0.1:8001",
      "X-CSRF-Token": auth.csrf_token,
    },
    data: {
      username: "settings-viewer",
      display_name: "Settings viewer",
      password: "viewer test password",
      role: "viewer",
    },
  });
  expect(created.ok()).toBe(true);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.getByLabel("Username", { exact: true }).fill("settings-viewer");
  await page
    .getByLabel("Password", { exact: true })
    .fill("viewer test password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/onboarding$/);
  await expect(
    page.getByRole("navigation", { name: "Setup steps" }).getByRole("button"),
  ).toHaveCount(2);
  await page.getByRole("button", { name: "Finish later", exact: true }).click();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page.locator(".settings-section")).toHaveCount(1);
  await expect(
    page.getByRole("region", { name: "General", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Metadata", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("navigation", { name: "Settings categories" })
    .getByRole("link", { name: "Metadata", exact: true })
    .click();
  await expect(
    page.getByRole("region", { name: "Metadata", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Users & access" }),
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Add server" })).toHaveCount(0);
  expect(errors).toEqual([]);
});
