import { expect, test } from "./fixtures";
import type { components } from "../src/api/schema";
type Connection = components["schemas"]["DownloaderView"];

function connection(overrides: Partial<Connection> = {}): Connection {
  return {
    id: "00000000-0000-0000-0000-000000000001",
    name: "qBittorrent",
    kind: "qbittorrent",
    base_url: "http://qbittorrent:8080",
    enabled: true,
    has_credentials: false,
    generation: 1,
    status: "untested",
    last_error: null,
    last_success_at: null,
    version: null,
    save_path: "",
    category: "",
    mappings: [],
    mappings_current: false,
    ...overrides,
  };
}

test("save automatically tests, retains failed settings, and retests edits", async ({
  page,
}) => {
  let saved: Connection | null = null;
  let creates = 0,
    tests = 0;
  let fail = true;
  let releaseTest!: () => void;
  const gate = new Promise<void>((resolve) => {
    releaseTest = resolve;
  });
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) return route.fallback();
    const method = route.request().method();
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "admin",
          display_name: "Reader",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/downloaders" && method === "GET")
      data = saved ? [saved] : [];
    else if (path === "/api/downloaders" && method === "POST") {
      creates++;
      saved = connection({ ...route.request().postDataJSON() });
      data = saved;
    } else if (path === `/api/downloaders/${saved?.id}` && method === "PUT") {
      saved = connection({
        ...saved,
        ...route.request().postDataJSON(),
        generation: 2,
        status: "untested",
        last_error: null,
      });
      data = saved;
    } else if (path.endsWith("/test")) {
      tests++;
      await gate;
      if (fail) {
        saved = {
          ...saved!,
          status: "unavailable",
          last_error: "Cannot reach the download client.",
        };
        return route.fulfill({
          status: 502,
          json: { detail: saved.last_error },
        });
      }
      saved = {
        ...saved!,
        status: "connected",
        last_error: null,
        version: "v5.2.3",
        last_success_at: "2026-09-23T15:30:00Z",
        save_path: "/data/torrents/ebooks",
        mappings_current: true,
        mappings: [
          {
            download_root: "/data/torrents/ebooks",
            worker_path: "/data/torrents/ebooks",
          },
        ],
      };
      data = saved;
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#downloaders");
  await page
    .getByRole("button", { name: "Connect qBittorrent", exact: true })
    .click();
  const form = page.getByRole("form", {
    name: "qBittorrent connection settings",
  });
  await form
    .getByLabel("qBittorrent URL or IP address", { exact: true })
    .fill("http://qbittorrent:8080");
  await form.getByRole("button", { name: "Save & test connection" }).click();
  await expect(page.getByLabel("Connection test status")).toContainText(
    "Testing…",
  );
  await expect(form).toHaveCount(0);
  releaseTest();
  await expect(
    page.getByRole("article", { name: "qBittorrent" }),
  ).toContainText("Connection saved. Test unsuccessful.");
  expect(creates).toBe(1);
  expect(tests).toBe(1);
  fail = false;
  await page.getByRole("button", { name: "Edit downloader" }).click();
  await expect(
    form.getByLabel("qBittorrent URL or IP address", { exact: true }),
  ).toHaveValue("http://qbittorrent:8080");
  await form.getByRole("button", { name: "Save & test connection" }).click();
  await expect(page.getByLabel("Connection test status")).toContainText(
    "Saved & connected",
  );
  await expect(
    page.getByText("Same folder path · no translation needed"),
  ).toBeVisible();
  await expect(
    page.getByRole("form", { name: "Download path mapping" }),
  ).not.toBeVisible();
  expect(creates).toBe(1);
  expect(tests).toBe(2);
});

test("advanced mappings browse local volumes, save translations, and fit mobile", async ({
  page,
}, testInfo) => {
  let saved = connection({
    status: "connected",
    version: "v5.2.3",
    last_success_at: "2026-09-23T15:30:00Z",
    save_path: "/mnt/unraid/data/torrents/ebooks",
  });
  let tests = 0;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (!url.pathname.startsWith("/api/")) return route.fallback();
    let data: unknown = [];
    if (url.pathname === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "admin",
          display_name: "Reader",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "pending", step: 3, skipped: [] };
    else if (url.pathname === "/api/downloaders") data = [saved];
    else if (url.pathname === "/api/downloaders/folders") {
      const path = url.searchParams.get("path");
      data = {
        path,
        parent: path === "/data" ? null : "/data",
        directories:
          path === "/data/torrents/ebooks"
            ? []
            : path === "/data"
              ? ["/data/torrents/ebooks"]
              : ["/data"],
        truncated: false,
      };
    } else if (url.pathname.endsWith("/test")) {
      tests++;
      saved = { ...saved, status: "connected" };
      data = saved;
    } else if (route.request().method() === "PUT") {
      saved = {
        ...saved,
        ...route.request().postDataJSON(),
        generation: saved.generation + 1,
        mappings_current: true,
      };
      data = saved;
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#downloaders");
  const card = page.getByRole("article", { name: "qBittorrent" });
  await expect(
    card.getByText("Folder setup needed", { exact: true }),
  ).toBeVisible();
  await page.evaluate(() =>
    document.documentElement.setAttribute("data-theme", "dark"),
  );
  await card.screenshot({
    path: testInfo.outputPath("downloader-desktop.png"),
  });
  await card.getByText("Advanced · path mappings").click();
  await card.getByRole("button", { name: "Add mapping", exact: true }).click();
  await expect(
    card.getByLabel("Folder in qBittorrent 1", { exact: true }),
  ).toHaveValue(saved.save_path);
  await card.getByRole("button", { name: "Browse Dewarr folder 1" }).click();
  const dialog = page.getByRole("dialog", {
    name: "Choose Dewarr download folder",
  });
  await dialog
    .locator(".mounted-folder-list")
    .getByRole("button", { name: "/data", exact: true })
    .dblclick();
  await dialog
    .locator(".mounted-folder-list")
    .getByRole("button", { name: "ebooks", exact: true })
    .dblclick();
  await dialog.screenshot({
    path: testInfo.outputPath("download-folder-picker.png"),
  });
  await dialog.getByRole("button", { name: "Select folder" }).click();
  await expect(
    card.getByLabel("Same folder in Dewarr 1", { exact: true }),
  ).toHaveValue("/data/torrents/ebooks");
  await card.getByRole("button", { name: "Add mapping", exact: true }).click();
  await card.getByRole("button", { name: "Remove mapping 2" }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() =>
      page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    )
    .toBe(true);
  await card.screenshot({ path: testInfo.outputPath("downloader-mobile.png") });
  await card.getByRole("button", { name: "Save & test mappings" }).click();
  await expect(
    card.getByText("Folder mapping configured", { exact: true }),
  ).toBeVisible();
  await expect(card.getByText("Dewarr folder:")).toContainText(
    "/data/torrents/ebooks",
  );
  expect(tests).toBe(1);
  expect(saved.mappings).toEqual([
    {
      download_root: "/mnt/unraid/data/torrents/ebooks",
      worker_path: "/data/torrents/ebooks",
    },
  ]);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/onboarding");
  await expect(page.getByText("STEP 4 OF 7", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("article", { name: "qBittorrent" }),
  ).toContainText("Folder mapping configured");
  await page
    .locator(".onboarding-card")
    .screenshot({ path: testInfo.outputPath("downloader-onboarding.png") });
});
