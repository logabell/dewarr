import { expect, test } from "./fixtures";

test("MAM masks saved secrets and saves edited proxy before testing", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let connection = {
    configured: true,
    base_url: "https://www.myanonamouse.net",
    proxy_url: "http://192.0.2.10:8888",
    proxy_fallback_direct: true,
    has_session: true,
    has_proxy_credentials: true,
    enabled: true,
    generation: 1,
    status: "route",
    last_error: "Proxy refused the connection.",
  };
  const actions: string[] = [];
  const writes: Record<string, unknown>[] = [];
  let rejectSave = false;
  let rejectTest = false;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
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
    else if (path === "/api/sources/mam/connection") {
      if (route.request().method() === "PUT") {
        actions.push("save");
        const body = route.request().postDataJSON();
        writes.push(body);
        if (rejectSave)
          return route.fulfill({
            status: 409,
            json: { detail: "Settings changed; reload before saving." },
          });
        connection = {
          ...connection,
          proxy_url: body.proxy_url,
          proxy_fallback_direct: body.proxy_fallback_direct,
          generation: connection.generation + 1,
        };
      }
      data = connection;
    } else if (path === "/api/sources/mam/network/test") {
      actions.push("test");
      if (rejectTest) {
        connection = {
          ...connection,
          status: "authentication",
          last_error: "MAM rejected the test session.",
        };
        return route.fulfill({
          json: {
            connection,
            status: "unhealthy",
            route: "proxy",
            cookie_status: "rejected",
            proxy_status: "healthy",
            proxy: { ip: "203.0.113.10" },
            direct: { ip: "198.51.100.20" },
            checked_at: "2026-09-20T12:00:00Z",
            message: "MAM rejected the test session.",
          },
        });
      }
      connection = { ...connection, status: "connected", last_error: "" };
      data = {
        connection,
        status: "healthy",
        route: "proxy",
        cookie_status: "authenticated",
        proxy_status: "healthy",
        proxy: { ip: "203.0.113.10" },
        direct: { ip: "198.51.100.20" },
        checked_at: "2026-09-20T12:00:00Z",
        message: "MAM authenticated through the configured proxy.",
      };
    } else if (path.includes("/connection"))
      data = {
        configured: false,
        generation: 0,
        base_url: "",
        excluded_indexers: [],
      };
    if (
      new URL(route.request().url()).pathname.includes(
        "/acquisition/preferences/",
      )
    )
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#sources");
  await page
    .getByRole("region", { name: "MAM settings" })
    .locator("summary")
    .first()
    .click();
  const form = page.getByRole("form", { name: "MAM connection settings" });
  const cookie = form.getByLabel("mam_id", { exact: true });
  await expect(cookie).toHaveValue("");
  await expect(cookie).toHaveAttribute("placeholder", "••••••••");
  await expect(
    form.getByLabel("Proxy password", { exact: true }),
  ).toHaveAttribute("placeholder", "••••••••");
  await form.getByText("Proxy options", { exact: true }).click();
  const proxy = form.locator('input[type="url"]').nth(1);
  await expect(proxy).toBeVisible();
  await expect(
    form.getByRole("checkbox", {
      name: "Allow direct fallback when the proxy is unavailable",
    }),
  ).toBeChecked();
  const automation = form.locator("details.account-automation");
  await expect(automation).not.toHaveAttribute("open", "");
  await automation.locator("summary").click();
  await expect(
    form.getByRole("checkbox", {
      name: "Use a Freeleech wedge on download",
    }),
  ).not.toBeChecked();
  await form.getByRole("checkbox", { name: "Protect minimum ratio" }).check();
  await expect(form.getByLabel("If ratio falls below")).toHaveValue("2.5");
  await expect(form.getByLabel("Check interval (hours)")).toHaveValue("3");
  await form.getByRole("checkbox", { name: "Protect minimum ratio" }).uncheck();
  await form
    .getByRole("button", { name: "Test connection", exact: true })
    .click();
  await expect(
    form.getByRole("status", { name: "Connection test status" }),
  ).toContainText("healthy");
  expect(actions).toEqual(["test"]);
  const network = form.getByRole("region", { name: "MAM network status" });
  await expect(network).toContainText("authenticated");
  await expect(network).toContainText("203.0.113.10");
  await expect(network).toContainText("198.51.100.20");
  await proxy.fill("http://proxy.internal:8888");
  await expect(network).toContainText("Not tested");
  await expect(network).not.toContainText("203.0.113.10");
  await form
    .getByRole("button", { name: "Save & test connection", exact: true })
    .click();
  await expect(
    form.getByRole("button", { name: "Test connection", exact: true }),
  ).toBeEnabled();
  expect(actions).toEqual(["test", "save", "test"]);
  expect(writes[0]).toMatchObject({
    proxy_url: "http://proxy.internal:8888",
    mam_id: null,
    proxy_password: null,
  });
  await network.screenshot({
    path: testInfo.outputPath("mam-network-healthy.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(network).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.reload();
  await page
    .getByRole("region", { name: "MAM settings" })
    .locator("summary")
    .first()
    .click();
  await form.getByText("Proxy options", { exact: true }).click();
  await expect(cookie).toHaveAttribute("placeholder", "••••••••");
  await expect(cookie).toHaveValue("");
  await expect(proxy).toHaveValue("http://proxy.internal:8888");
  await automation.locator("summary").click();
  await form.screenshot({
    path: testInfo.outputPath("mam-masked-settings.png"),
  });
  await form
    .getByRole("checkbox", { name: "Use a Freeleech wedge on download" })
    .check();
  const retainedCookie = "keep-this-cookie-after-a-failed-test";
  await cookie.fill(retainedCookie);
  rejectTest = true;
  const failedTestButton = form.locator('.actions button[type="button"]');
  await expect(failedTestButton).toHaveText("Save & test connection");
  await failedTestButton.click();
  await expect(
    form.getByRole("status", { name: "Connection test status" }),
  ).toContainText("unhealthy");
  await expect(
    form.getByRole("status", { name: "Connection test status" }),
  ).toContainText("MAM rejected the test session.");
  await expect(failedTestButton).toBeInViewport();
  await expect(cookie).toHaveValue(retainedCookie);
  rejectTest = false;
  rejectSave = true;
  await proxy.fill("http://another-proxy:8888");
  await form
    .getByRole("button", { name: "Save & test connection", exact: true })
    .click();
  await expect(form).toContainText("Settings changed; reload before saving.");
  expect(actions).toEqual(["test", "save", "test", "save", "test", "save"]);
  expect(writes.at(-1)).toMatchObject({
    automation: {
      seedbox_ip: false,
      auto_vip: false,
      use_wedge: true,
      protect_ratio: false,
      maintain_buffer: false,
      spend_bonus: false,
    },
  });
  expect(errors).toEqual([]);
});
