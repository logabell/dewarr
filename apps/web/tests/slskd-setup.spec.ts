import { expect, test } from "./fixtures";

test("Soulseek saves and tests, appears as a client, and maps through the native folder browser", async ({
  page,
}, testInfo) => {
  let source = {
    configured: false,
    enabled: false,
    base_url: "http://slskd:5030",
    has_api_key: false,
    generation: 0,
    status: "not-configured",
    last_error: null,
    last_success_at: null as string | null,
    download_root: null as string | null,
    mapped: false,
    downloader_id: "slskd-client",
    downloader_generation: 1,
  };
  let mappings: { download_root: string; worker_path: string }[] = [];
  let tests = 0;
  const downloader = () => ({
    id: "slskd-client",
    kind: "slskd",
    name: "Soulseek",
    base_url: source.base_url,
    enabled: source.enabled,
    has_credentials: source.has_api_key,
    generation: source.downloader_generation,
    status: source.status,
    last_error: null,
    last_success_at: source.last_success_at,
    version: "0.26",
    save_path: source.download_root || "",
    category: "",
    mappings,
    mappings_current: source.mapped,
    capabilities: {},
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url()),
      path = url.pathname;
    if (!path.startsWith("/api/")) return route.fallback();
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "admin",
          role: "admin",
          display_name: "Reader",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/setup/readiness")
      data = { download_dispatch_enabled: false };
    else if (path === "/api/downloaders")
      data = source.configured ? [downloader()] : [];
    else if (path === "/api/sources/slskd/connection") {
      if (route.request().method() === "PUT")
        source = {
          ...source,
          ...route.request().postDataJSON(),
          configured: true,
          has_api_key: true,
          generation: source.generation + 1,
          status: "untested",
        };
      data = source;
    } else if (path.endsWith("/test")) {
      tests++;
      source = {
        ...source,
        status: "connected",
        last_success_at: "2026-09-24T12:00:00Z",
        download_root: "/media/downloads",
      };
      data = path.startsWith("/api/downloaders") ? downloader() : source;
    } else if (path.endsWith("/mappings")) {
      mappings = route.request().postDataJSON().mappings;
      source = {
        ...source,
        mapped: true,
        downloader_generation: source.downloader_generation + 1,
      };
      data = downloader();
    } else if (path === "/api/downloaders/folders") {
      const folder = url.searchParams.get("path");
      if (folder === "/missing")
        return route.fulfill({
          status: 422,
          json: { detail: "Choose a readable folder inside a mounted volume." },
        });
      data = {
        path: folder,
        parent: folder === "/data" ? null : "/data",
        directories: !folder
          ? ["/data"]
          : folder === "/data"
            ? ["/data/downloads", "/data/library"]
            : [],
        truncated: false,
      };
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#downloaders");
  await page.evaluate(() =>
    document.documentElement.setAttribute("data-theme", "dark"),
  );
  await expect(
    page.getByText("Downloads are disabled on this server", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Connect Soulseek", exact: true })
    .click();
  const connection = page.getByRole("dialog", { name: "Soulseek connection" });
  await connection
    .getByLabel("API key", { exact: true })
    .fill("private-slskd-test-key");
  await connection
    .getByRole("button", { name: "Save & test connection" })
    .click();
  await expect(connection.getByLabel("Connection test status")).toContainText(
    "connected",
  );
  expect(tests).toBe(1);
  await connection
    .getByRole("button", { name: "Close soulseek connection" })
    .click();
  const card = page.getByRole("article", { name: "Soulseek", exact: true });
  await expect(
    card.getByText("Folder setup needed", { exact: true }),
  ).toBeVisible();
  await card.getByText("Advanced · path mappings").click();
  await card.getByRole("button", { name: "Add mapping", exact: true }).click();
  await card.getByRole("button", { name: "Browse Dewarr folder 1" }).click();
  const browser = page.getByRole("dialog", {
    name: "Choose Dewarr download folder",
  });
  const rows = browser.locator(".mounted-folder-list");
  await rows.getByRole("button", { name: "/data", exact: true }).click();
  await expect(
    rows.getByRole("button", { name: "/data", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await browser.getByRole("button", { name: "Open", exact: true }).click();
  await rows.getByRole("button", { name: "downloads", exact: true }).focus();
  await page.keyboard.press("ArrowDown");
  await expect(
    rows.getByRole("button", { name: "library", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await page.keyboard.press("Enter");
  await expect(
    browser.getByText("This folder is empty. You can select it below."),
  ).toBeVisible();
  await browser.getByRole("button", { name: "Back in folders" }).click();
  await expect(
    rows.getByRole("button", { name: "downloads", exact: true }),
  ).toBeVisible();
  await browser.getByRole("button", { name: "Forward in folders" }).click();
  await browser.getByRole("button", { name: "Up one level" }).click();
  await rows.getByRole("button", { name: "downloads", exact: true }).click();
  await browser.getByRole("button", { name: "Enter folder path" }).click();
  await browser.getByRole("textbox", { name: "Folder path" }).fill("/missing");
  await browser.getByRole("button", { name: "Go to folder" }).click();
  await expect(
    browser.getByText("Choose a readable folder inside a mounted volume."),
  ).toBeVisible();
  await expect(
    browser.getByRole("button", { name: "Select folder", exact: true }),
  ).toBeDisabled();
  await browser.getByRole("button", { name: "Back in folders" }).click();
  await rows.getByRole("button", { name: "downloads", exact: true }).click();
  await browser.screenshot({
    path: testInfo.outputPath("native-explorer-desktop.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await browser.screenshot({
    path: testInfo.outputPath("native-explorer-mobile.png"),
  });
  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    )
    .toBe(true);
  await browser
    .getByRole("button", { name: "Select folder", exact: true })
    .click();
  await expect(
    card.getByLabel("Same folder in Dewarr 1", { exact: true }),
  ).toHaveValue("/data/downloads");
  await card.getByRole("button", { name: "Save & test mappings" }).click();
  await expect(
    card.getByText("Folder mapping configured", { exact: true }),
  ).toBeVisible();
  expect(mappings).toEqual([
    { download_root: "/media/downloads", worker_path: "/data/downloads" },
  ]);
  expect(tests).toBe(2);
});
