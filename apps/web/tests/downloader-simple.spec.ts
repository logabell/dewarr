import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test.afterEach(() => {
  execFileSync("uv", ["run", "python", "scripts/e2e_downloader_cleanup.py"], {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    stdio: "pipe",
    timeout: 10_000,
  });
});

test("qBittorrent setup only needs an address and category", async ({
  page,
}, testInfo) => {
  execFileSync("uv", ["run", "python", "scripts/e2e_auth_budget.py"], {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    stdio: "pipe",
  });
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
    page.getByRole("button", { name: "Sign out", exact: true }),
  ).toBeVisible();
  await page.goto("/library?view=saved");
  await expect(
    page.getByRole("button", { name: "Sign out", exact: true }),
  ).toBeVisible();
  let connectionTests = 0;
  // These example addresses are intentionally offline. Keep the settings
  // workflow deterministic while exercising the automatic test request.
  await page.route("**/api/downloaders/*/test", async (route) => {
    connectionTests++;
    await route.fulfill({
      status: 502,
      json: { detail: "The example download client is offline." },
    });
  });
  let rootsRequested = false;
  await page.route("**/api/organization/download-roots", async (route) => {
    rootsRequested = true;
    await route.abort();
  });
  await page.goto("/settings#downloaders");
  await page
    .getByRole("button", { name: "Connect qBittorrent", exact: true })
    .click();
  const form = page.getByRole("form", {
    name: "qBittorrent connection settings",
  });
  await expect(form.locator("input")).toHaveCount(4);
  await form
    .getByLabel("qBittorrent URL or IP address", { exact: true })
    .fill("10.0.0.2:8080");
  await form
    .getByLabel("Download category", { exact: true })
    .fill("simple-books");
  await expect(
    form.getByLabel("qBittorrent username (optional)"),
  ).not.toHaveAttribute("required");
  await expect(
    form.getByLabel("qBittorrent password (optional)"),
  ).not.toHaveAttribute("required");
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/downloaders") &&
      response.request().method() === "POST",
  );
  await form
    .getByRole("button", { name: "Save & test connection", exact: true })
    .click();
  const response = await saved;
  expect(response.status()).toBe(201);
  expect(response.request().postDataJSON()).not.toHaveProperty("save_path");
  expect(response.request().postDataJSON()).not.toHaveProperty("mappings");
  await expect(form).toHaveCount(0);
  await page.reload();
  const card = page
    .getByRole("article")
    .filter({ hasText: "http://10.0.0.2:8080" });
  await expect(card).toContainText("simple-books");
  await card
    .getByRole("button", { name: "Edit downloader", exact: true })
    .click();
  await expect(form.locator("input")).toHaveCount(4);
  await expect(
    form.getByLabel("Download category", { exact: true }),
  ).toHaveValue("simple-books");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("simple-qbittorrent-mobile.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page
    .getByRole("button", { name: "Connect SABnzbd", exact: true })
    .click();
  const sab = page.getByRole("form", { name: "SABnzbd connection settings" });
  await expect(sab.locator("input")).toHaveCount(3);
  await expect(
    sab.getByLabel("SABnzbd API key", { exact: true }),
  ).toHaveAttribute("required", "");
  await sab
    .getByLabel("SABnzbd URL or IP address", { exact: true })
    .fill("10.0.0.3:8080");
  await sab
    .getByLabel("SABnzbd API key", { exact: true })
    .fill("browser-sab-key");
  await sab
    .getByLabel("Download category", { exact: true })
    .fill("usenet-books");
  const sabSaved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/downloaders") &&
      response.request().method() === "POST",
  );
  await sab
    .getByRole("button", { name: "Save & test connection", exact: true })
    .click();
  const sabResponse = await sabSaved;
  expect(sabResponse.status()).toBe(201);
  expect(sabResponse.request().postDataJSON()).toMatchObject({
    kind: "sabnzbd",
    api_key: "browser-sab-key",
    category: "usenet-books",
  });
  const sabBody = await sabResponse.json();
  expect(sabBody.kind).toBe("sabnzbd");
  expect(JSON.stringify(sabBody)).not.toContain("browser-sab-key");
  await expect(sab).toHaveCount(0);
  const sabCard = page
    .getByRole("article")
    .filter({ hasText: "http://10.0.0.3:8080" });
  await expect(sabCard).toContainText("usenet-books");
  await page
    .getByRole("button", { name: "Connect NZBGet", exact: true })
    .click();
  const nzb = page.getByRole("form", { name: "NZBGet connection settings" });
  await expect(nzb.locator("input")).toHaveCount(4);
  await expect(
    nzb.getByLabel("NZBGet username (optional)", { exact: true }),
  ).not.toHaveAttribute("required");
  await expect(
    nzb.getByLabel("NZBGet password (optional)", { exact: true }),
  ).not.toHaveAttribute("required");
  await nzb
    .getByLabel("NZBGet URL or IP address", { exact: true })
    .fill("10.0.0.4:6789");
  await nzb
    .getByLabel("NZBGet username (optional)", { exact: true })
    .fill("nzb-user");
  await nzb
    .getByLabel("NZBGet password (optional)", { exact: true })
    .fill("browser-nzb-password");
  await nzb.getByLabel("Download category", { exact: true }).fill("nzb-books");
  const nzbSaved = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/downloaders") &&
      response.request().method() === "POST",
  );
  await nzb
    .getByRole("button", { name: "Save & test connection", exact: true })
    .click();
  const nzbResponse = await nzbSaved;
  expect(nzbResponse.status()).toBe(201);
  expect(nzbResponse.request().postDataJSON()).toMatchObject({
    kind: "nzbget",
    username: "nzb-user",
    password: "browser-nzb-password",
    category: "nzb-books",
  });
  const nzbBody = await nzbResponse.json();
  expect(nzbBody.kind).toBe("nzbget");
  expect(JSON.stringify(nzbBody)).not.toContain("browser-nzb-password");
  expect(JSON.stringify(nzbBody)).not.toContain("nzb-user");
  await expect(nzb).toHaveCount(0);
  const nzbCard = page
    .getByRole("article")
    .filter({ hasText: "http://10.0.0.4:6789" });
  await expect(nzbCard).toContainText("nzb-books");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
  expect(rootsRequested).toBe(false);
  expect(connectionTests).toBe(3);
});
