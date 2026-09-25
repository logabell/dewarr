import { expect, test, type Page } from "../fixtures";

const checked = "2026-09-24T19:00:00Z";
async function setup(page: Page) {
  const state = {
    fail: false,
    connections: [
      {
        key: "mam",
        name: "MAM account",
        status: "authentication",
        message:
          "MAM rejected mam_id. Check the proxy IP and renew the session for the current connection.",
        checked_at: checked,
        settings_url: "/settings#sources",
      },
      {
        key: "mam-proxy",
        name: "MAM proxy",
        status: "unavailable",
        message: "MAM proxy failed; using the direct fallback.",
        checked_at: checked,
        settings_url: "/settings#sources",
      },
      {
        key: "qbit",
        name: "Books qBit",
        status: "stale",
        message:
          "Connection check is overdue. Check that the background worker is running.",
        checked_at: checked,
        settings_url: "/settings#downloaders",
      },
    ],
  };
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
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
    else if (path === "/api/health/connections") {
      if (state.fail)
        return route.fulfill({ status: 503, json: { detail: "Unavailable" } });
      data = {
        connections: state.connections,
        issues: state.connections.filter((c) => c.status !== "connected")
          .length,
        check_interval_seconds: 300,
      };
    }
    return route.fulfill({ json: data });
  });
  await page.clock.install();
  await page.goto("/settings#display");
  return state;
}

test("top bar explains connection failures and links to settings", async ({
  page,
}) => {
  await setup(page);
  const warning = page.getByLabel("3 connection issues", { exact: true });
  await expect(warning).toBeVisible();
  await warning.focus();
  await page.keyboard.press("Enter");
  const panel = page.getByLabel("Connection issues", { exact: true });
  await expect(panel.getByText("MAM account", { exact: true })).toBeVisible();
  await expect(panel.getByText(/MAM rejected mam_id/)).toBeVisible();
  await expect(panel.getByText(/using the direct fallback/)).toBeVisible();
  await expect(panel.getByText(/Connection check is overdue/)).toBeVisible();
  await expect(
    panel.getByRole("link", { name: "Open settings" }).last(),
  ).toHaveAttribute("href", "/settings#downloaders");
  await page.screenshot({ path: "/tmp/dewarr-connection-warning.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  const box = await panel.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
});

test("polling clears recovered issues and reports an unavailable health API", async ({
  page,
}) => {
  const state = await setup(page);
  await expect(
    page.getByLabel("3 connection issues", { exact: true }),
  ).toBeVisible();
  state.connections = state.connections.map((item) => ({
    ...item,
    status: "connected",
    message: "Connection verified.",
  }));
  await page.clock.fastForward(31_000);
  await expect(page.locator(".connection-health")).toHaveCount(0);
  state.fail = true;
  await page.clock.fastForward(31_000);
  await page.clock.runFor(2_000);
  await expect(
    page.getByLabel("Connection status unavailable", { exact: true }),
  ).toBeVisible();
  state.fail = false;
  await page.clock.fastForward(31_000);
  await expect(page.locator(".connection-health")).toHaveCount(0);
});
