import { expect, test, type Page } from "./fixtures";

async function setupMam(page: Page, configured = true) {
  const state = {
    connection: {
      configured,
      base_url: "https://www.myanonamouse.net",
      proxy_url: configured ? "http://gluetun:8888" : "",
      proxy_fallback_direct: false,
      has_session: configured,
      has_proxy_credentials: configured,
      enabled: configured,
      generation: configured ? 1 : 0,
      status: configured ? "untested" : "not-configured",
      last_error: "",
      last_checked_at: null as string | null,
      proxy_health: {
        status: "untested",
        checked_at: null as string | null,
        ip: null as string | null,
        message: "",
      },
      automation: {} as Record<string, unknown>,
    },
    actions: [] as string[],
    writes: [] as Record<string, unknown>[],
    rejectCookie: false,
    rejectSave: false,
    proxyError: "",
  };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
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
        state.actions.push("save");
        const body = route.request().postDataJSON();
        state.writes.push(body);
        if (state.rejectSave)
          return route.fulfill({
            status: 409,
            json: { detail: "Settings changed; reload before saving." },
          });
        expect(body.expected_generation).toBe(state.connection.generation);
        state.connection = {
          ...state.connection,
          configured: true,
          base_url: body.base_url,
          proxy_url: body.proxy_url || "",
          proxy_fallback_direct: body.proxy_fallback_direct,
          has_session: Boolean(body.mam_id || state.connection.has_session),
          has_proxy_credentials: Boolean(
            body.proxy_password || state.connection.has_proxy_credentials,
          ),
          enabled: body.enabled,
          generation: state.connection.generation + 1,
          status: "untested",
          last_error: "",
          automation: body.automation,
        };
      }
      data = state.connection;
    } else if (path === "/api/sources/mam/network/test") {
      state.actions.push("proxy");
      expect(url.searchParams.get("include_cookie")).toBe("false");
      data = {
        connection: state.connection,
        status: state.proxyError ? "unhealthy" : "healthy",
        route: "proxy",
        cookie_status: "not-tested",
        proxy_status: state.proxyError ? "unavailable" : "healthy",
        proxy: state.proxyError
          ? { error: state.proxyError }
          : { ip: "203.0.113.10" },
        direct: { ip: "198.51.100.20" },
        checked_at: "2026-09-24T12:00:00Z",
        message: "Network checks completed.",
      };
    } else if (path === "/api/sources/mam/connection/test") {
      state.actions.push("cookie");
      state.connection.status = state.rejectCookie
        ? "authentication"
        : "connected";
      state.connection.last_error = state.rejectCookie
        ? "MAM rejected the test session."
        : "";
      if (state.rejectCookie)
        return route.fulfill({
          status: 422,
          json: { detail: state.connection.last_error },
        });
      data = state.connection;
    } else if (path.includes("/connection"))
      data = {
        configured: false,
        generation: 0,
        base_url: "",
        excluded_indexers: [],
      };
    else if (path.includes("/acquisition/preferences/"))
      data = { effective: { desired_media: "both" } };
    await route.fulfill({ json: data });
  });
  await page.goto("/settings#sources");
  await page
    .getByRole("region", { name: "MAM settings" })
    .locator("summary")
    .first()
    .click();
  return state;
}

const formFor = (page: Page) =>
  page.getByRole("form", { name: "MAM connection settings" });

test("proxy setup works without a cookie and both tests precede advanced settings", async ({
  page,
}) => {
  const state = await setupMam(page, false);
  const form = formFor(page);
  await expect(
    form.getByRole("button", { name: "Test mam_id", exact: true }),
  ).toBeDisabled();
  await expect(form.locator("details.mam-advanced")).not.toHaveAttribute(
    "open",
    "",
  );
  await form
    .getByLabel("HTTP proxy URL", { exact: true })
    .fill("http://gluetun:8888");
  await form.getByRole("button", { name: "Test proxy", exact: true }).click();
  const network = form.getByRole("region", { name: "MAM network status" });
  await expect(network).toContainText("Healthy");
  await expect(network).toContainText("203.0.113.10");
  await expect(network).toContainText("198.51.100.20");
  expect(state.actions).toEqual(["save", "proxy"]);
  expect(state.writes[0].mam_id).toBeNull();
  const advancedBox = await form.locator("details.mam-advanced").boundingBox();
  for (const name of ["Test proxy", "Test mam_id"]) {
    const buttonBox = await form
      .getByRole("button", { name, exact: true })
      .boundingBox();
    expect(buttonBox!.y + buttonBox!.height).toBeLessThan(advancedBox!.y);
  }
});

test("proxy and cookie tests are independent, including failed sign-in and retained IPs", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const state = await setupMam(page);
  const form = formFor(page);
  const cookie = form.getByLabel("mam_id", { exact: true });
  const network = form.getByRole("region", { name: "MAM network status" });
  const account = form.getByRole("region", { name: "MAM account check" });
  await expect(cookie).toHaveValue("");
  await expect(cookie).toHaveAttribute("placeholder", "••••••••");
  await form.getByRole("button", { name: "Test proxy", exact: true }).click();
  await expect(network).toContainText("Healthy");
  await expect(account).toContainText("Unverified");
  expect(state.actions).toEqual(["proxy"]);
  await cookie.fill("replacement-test-cookie");
  await expect(network).toContainText("203.0.113.10");
  state.rejectCookie = true;
  await form.getByRole("button", { name: "Test mam_id", exact: true }).click();
  await expect(account).toContainText(
    "MAM sign-in failed. Check your mam_id and its allowed IP.",
  );
  await expect(
    account.getByText("MAM rejected the test session.", { exact: true }),
  ).not.toBeVisible();
  await expect(network).toContainText("Healthy");
  await expect(network).toContainText("203.0.113.10");
  await expect(cookie).toHaveValue("replacement-test-cookie");
  expect(state.actions).toEqual(["proxy", "save", "cookie"]);
  await form.screenshot({
    path: testInfo.outputPath("mam-independent-checks.png"),
  });
  state.rejectCookie = false;
  await form.getByRole("button", { name: "Test mam_id", exact: true }).click();
  await expect(account).toContainText("Authenticated");
  await expect(cookie).toHaveValue("");
  expect(state.actions).toEqual(["proxy", "save", "cookie", "cookie"]);
  await form.getByRole("button", { name: "Test proxy", exact: true }).click();
  await expect(account).toContainText("Authenticated");
  await expect(network).toContainText("Healthy");
  expect(state.actions.at(-1)).toBe("proxy");
  expect(errors).toEqual([]);
});

test("proxy DNS error is short, detailed on demand, and fits a phone", async ({
  page,
}, testInfo) => {
  const state = await setupMam(page);
  state.proxyError =
    "Public IP lookup failed. The proxy hostname could not be resolved. For a Docker service name such as gluetun, connect Dewarr and the proxy to the same Docker network.";
  const form = formFor(page);
  await form.getByRole("button", { name: "Test proxy", exact: true }).click();
  const network = form.getByRole("region", { name: "MAM network status" });
  await expect(
    network.getByText(
      "Proxy not found. Connect Dewarr and Gluetun to the same Docker network.",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    network.getByText(state.proxyError, { exact: true }),
  ).not.toBeVisible();
  await expect(network).toContainText("198.51.100.20");
  await network.getByText("Error details", { exact: true }).click();
  await expect(
    network.getByText(state.proxyError, { exact: true }),
  ).toBeVisible();
  await network.getByText("Error details", { exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await form.screenshot({ path: testInfo.outputPath("mam-mobile-error.png") });
  const account = form.getByRole("region", { name: "MAM account check" });
  expect((await account.boundingBox())!.y).toBeGreaterThan(
    (await network.boundingBox())!.y,
  );
});

test("advanced credentials and automation save before testing, with save conflicts preserved", async ({
  page,
}) => {
  const state = await setupMam(page);
  const form = formFor(page);
  await form.getByText("Advanced settings", { exact: true }).click();
  await expect(
    form.getByLabel("Proxy password", { exact: true }),
  ).toHaveAttribute("placeholder", "••••••••");
  await form
    .getByLabel("Proxy username", { exact: true })
    .fill("new-proxy-user");
  await form
    .getByLabel("Proxy password", { exact: true })
    .fill("new-proxy-password");
  await form.locator("details.account-automation > summary").click();
  await form
    .getByRole("checkbox", { name: "Use a Freeleech wedge on download" })
    .check();
  await form.getByRole("button", { name: "Test proxy", exact: true }).click();
  await expect(
    form.getByRole("region", { name: "MAM network status" }),
  ).toContainText("Healthy");
  expect(state.actions).toEqual(["save", "proxy"]);
  expect(state.writes[0]).toMatchObject({
    proxy_username: "new-proxy-user",
    proxy_password: "new-proxy-password",
    mam_id: null,
    automation: { use_wedge: true },
  });
  await expect(form.getByLabel("Proxy password", { exact: true })).toHaveValue(
    "",
  );
  state.rejectSave = true;
  await form
    .getByLabel("HTTP proxy URL", { exact: true })
    .fill("http://another-proxy:8888");
  await form.getByRole("button", { name: "Test proxy", exact: true }).click();
  await expect(form).toContainText("Settings changed; reload before saving.");
  expect(state.actions).toEqual(["save", "proxy", "save"]);
});

test("background results expire cached proxy success and clear old cookie errors", async ({
  page,
}) => {
  await page.clock.install();
  const state = await setupMam(page);
  const form = formFor(page);
  const network = form.getByRole("region", { name: "MAM network status" });
  await form.getByRole("button", { name: "Test proxy", exact: true }).click();
  await expect(network).toContainText("Healthy");
  state.rejectCookie = true;
  await form.getByRole("button", { name: "Test mam_id", exact: true }).click();
  const account = form.getByRole("region", { name: "MAM account check" });
  await expect(account).toContainText("Failed");
  state.connection.proxy_health = {
    status: "stale",
    checked_at: "2026-09-24T11:59:59Z",
    ip: null,
    message: "Check overdue",
  };
  state.connection.status = "connected";
  state.connection.last_error = "";
  state.connection.last_checked_at = new Date(
    Date.now() + 30_000,
  ).toISOString();
  await page.clock.fastForward(31_000);
  await expect(network).toContainText("Check overdue");
  await expect(network).not.toContainText("Healthy");
  await expect(account).toContainText("Authenticated");
  await expect(account).not.toContainText("Failed");
});
