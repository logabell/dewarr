import { expect, test, type Page } from "../fixtures";

async function setup(page: Page) {
  const state = {
    account: {
      username: "LibraryMouse",
      uid: "42",
      classname: "Power User",
      ratio: "3.25",
      uploaded: "1.25 TiB",
      downloaded: "393.85 GiB",
      seedbonus: 75000,
      checked_at: "2026-09-24T12:30:00Z",
      vip_until: null,
    },
    connection: {
      configured: true,
      enabled: true,
      has_session: true,
      has_proxy_credentials: false,
      generation: 1,
      status: "connected",
      base_url: "https://www.myanonamouse.net",
      proxy_url: null,
      proxy_fallback_direct: false,
      automation: {},
      last_error: null,
      proxy_health: {},
    },
    purchases: [] as Record<string, unknown>[],
    writes: [] as Record<string, unknown>[],
    accountError: false,
    outcome: "completed",
  };
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/auth/me")
      return route.fulfill({
        json: {
          user: {
            id: "reader",
            role: "admin",
            display_name: "Reader",
            onboarding_status: "complete",
          },
          csrf_token: "test",
        },
      });
    if (path === "/api/setup/onboarding")
      return route.fulfill({ json: { status: "completed" } });
    if (path === "/api/sources/mam/connection") {
      if (route.request().method() === "PUT") {
        const body = route.request().postDataJSON();
        state.writes.push(body);
        state.connection = {
          ...state.connection,
          ...body,
          generation: state.connection.generation + 1,
        };
      }
      return route.fulfill({ json: state.connection });
    }
    if (path === "/api/sources/mam/account")
      return route.fulfill(
        state.accountError
          ? { status: 422, json: { detail: "MAM rejected this session." } }
          : { json: state.account },
      );
    if (path === "/api/sources/mam/purchases") {
      state.purchases.push(route.request().postDataJSON());
      if (state.outcome === "completed") state.account.seedbonus = 25000;
      return route.fulfill({
        json: {
          status: state.outcome,
          message:
            state.outcome === "completed"
              ? "MAM confirmed your purchase."
              : "MAM did not confirm the outcome. Check your account on MAM before buying again.",
        },
      });
    }
    if (path.includes("/acquisition/preferences/"))
      return route.fulfill({ json: { effective: { desired_media: "both" } } });
    if (path.includes("/connection"))
      return route.fulfill({
        json: {
          configured: false,
          enabled: false,
          generation: 0,
          base_url: "",
          status: "not-configured",
          excluded_indexers: [],
        },
      });
    return route.fulfill({ json: [] });
  });
  await page.goto("/settings#sources");
  await page
    .getByRole("region", { name: "MAM settings", exact: true })
    .locator("summary")
    .first()
    .click();
  await expect(
    page.getByRole("heading", { name: "LibraryMouse" }),
  ).toBeVisible();
  return state;
}

const center = (page: Page) =>
  page.getByRole("region", { name: "MAM control center" });
const form = (page: Page) =>
  page.getByRole("form", { name: "MAM connection settings" });

test("account balances and reviewed purchase with refreshed balance", async ({
  page,
}, testInfo) => {
  const state = await setup(page);
  const panel = center(page);
  await expect(panel).toContainText("3.25");
  await expect(panel).toContainText("75,000");
  await expect(panel).toContainText("1.25 TiB");
  await expect(panel).toContainText("393.85 GiB");
  await panel
    .getByRole("combobox", { name: "Upload credit amount", exact: true })
    .selectOption("100");
  await expect(panel).toContainText("50,000 points");
  await page.screenshot({
    path: testInfo.outputPath("mam-desktop.png"),
    fullPage: true,
  });
  await panel
    .getByRole("button", { name: "Review purchase", exact: true })
    .click();
  expect(state.purchases).toHaveLength(0);
  await expect(
    panel.getByRole("group", { name: "Review MAM purchase" }),
  ).toContainText("100 GiB for 50,000 points");
  await panel.getByRole("button", { name: "Confirm purchase" }).click();
  await expect(panel).toContainText("MAM confirmed your purchase.");
  expect(state.purchases).toHaveLength(1);
  expect(state.purchases[0]).toMatchObject({
    expected_generation: 1,
    kind: "upload",
    amount: 100,
  });
  expect(state.purchases[0].request_id).toMatch(/^[a-f0-9-]{36}$/);
  await expect(
    panel.getByRole("button", { name: "Review purchase" }),
  ).toBeDisabled();
  await panel.getByRole("button", { name: "Refresh account" }).click();
  await panel
    .getByRole("combobox", { name: "Upload credit amount", exact: true })
    .selectOption("50");
  await expect(
    panel.getByRole("button", { name: "Review purchase" }),
  ).toBeEnabled();
});

test("editable ratio drafts, valid purchase presets, and invalid fields block saving", async ({
  page,
}) => {
  const state = await setup(page);
  const settings = form(page);
  await settings.locator("details.account-automation > summary").click();
  await settings
    .getByRole("checkbox", { name: "Protect minimum ratio", exact: true })
    .check();
  const ratio = settings.getByLabel("If ratio falls below", { exact: true });
  await ratio.fill("");
  await expect(ratio).toHaveValue("");
  await settings
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  expect(state.writes).toHaveLength(0);
  await ratio.fill("1.25");
  await settings
    .getByRole("combobox", { name: "Ratio rule purchase", exact: true })
    .selectOption("max");
  await settings
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect.poll(() => state.writes.length).toBe(1);
  expect(state.writes[0].automation).toMatchObject({
    protect_ratio: true,
    ratio_below: 1.25,
    ratio_buy_gb: "max",
  });
  await settings
    .getByRole("combobox", { name: "Ratio rule purchase", exact: true })
    .selectOption("custom");
  await settings
    .getByLabel("Ratio rule purchase — custom GiB", { exact: true })
    .fill("49");
  await settings
    .getByRole("button", { name: "Test network", exact: true })
    .click();
  expect(state.writes).toHaveLength(1);
  await settings
    .getByRole("checkbox", { name: "Protect minimum ratio", exact: true })
    .uncheck();
  await settings
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await expect.poll(() => state.writes.length).toBe(2);
  expect(state.writes[1].automation).toMatchObject({
    protect_ratio: false,
    ratio_buy_gb: 50,
  });
});

test("invalid and unaffordable purchases, unknown outcomes and connection edits", async ({
  page,
}) => {
  const state = await setup(page);
  const panel = center(page);
  await panel
    .getByRole("combobox", { name: "Upload credit amount", exact: true })
    .selectOption("custom");
  await panel
    .getByLabel("Upload credit amount — custom GiB", { exact: true })
    .fill("49");
  await expect(
    panel.getByRole("button", { name: "Review purchase" }),
  ).toBeDisabled();
  await panel
    .getByRole("combobox", { name: "Upload credit amount", exact: true })
    .selectOption("500");
  await expect(panel).toContainText("insufficient balance");
  await expect(
    panel.getByRole("button", { name: "Review purchase" }),
  ).toBeDisabled();
  await panel
    .getByRole("combobox", { name: "Upload credit amount", exact: true })
    .selectOption("max");
  state.outcome = "unknown";
  await panel.getByRole("button", { name: "Review purchase" }).click();
  await panel.getByRole("button", { name: "Confirm purchase" }).click();
  await expect(panel).toContainText(
    "Check your account on MAM before buying again.",
  );
  expect(state.purchases).toHaveLength(1);
  await expect(
    panel.getByRole("button", { name: "Review purchase" }),
  ).toBeDisabled();
  await form(page)
    .getByLabel("HTTP proxy URL", { exact: true })
    .fill("http://changed:8888");
  await expect(panel).toContainText("Save connection changes");
  await expect(
    panel.getByRole("heading", { name: "LibraryMouse" }),
  ).not.toBeVisible();
});

test("mobile layout, VIP and wedge selections, and failed refresh", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await setup(page);
  const panel = center(page);
  await panel
    .getByRole("combobox", { name: "Purchase", exact: true })
    .selectOption("VIP");
  await expect(panel).toContainText("seven days (1,250 points)");
  await panel.getByRole("button", { name: "Review purchase" }).click();
  await panel.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(state.purchases).toHaveLength(0);
  await panel
    .getByRole("combobox", { name: "Purchase", exact: true })
    .selectOption("wedges");
  await expect(panel).toContainText("does not apply it to a torrent");
  await page.screenshot({
    path: testInfo.outputPath("mam-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  state.accountError = true;
  await panel.getByRole("button", { name: "Refresh account" }).click();
  await expect(panel.getByRole("alert")).toContainText(
    "last successful refresh",
  );
  await expect(
    panel.getByRole("button", { name: "Review purchase" }),
  ).toBeDisabled();
});
