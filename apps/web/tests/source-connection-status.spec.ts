import { expect, test } from "./fixtures";

test("source connections keep masked credentials and show verified status beside test actions", async ({
  page,
}) => {
  const checkedAt = "2026-09-23T15:30:00Z";
  const connections = {
    prowlarr: {
      configured: true,
      base_url: "http://prowlarr:9696",
      has_api_key: true,
      enabled: true,
      generation: 2,
      excluded_indexers: [],
      status: "untested",
      last_error: null,
      last_success_at: null as string | null,
    },
    slskd: {
      configured: true,
      enabled: true,
      base_url: "http://slskd:5030",
      has_api_key: true,
      generation: 3,
      status: "untested",
      last_error: null,
      last_success_at: null as string | null,
      download_root: "/downloads",
      mapped: true,
      downloader_id: "00000000-0000-0000-0000-000000000001",
      downloader_generation: 1,
    },
    audiobookbay: {
      configured: true,
      base_url: "https://audiobookbay.example",
      proxy_url: "http://proxy:8888",
      has_proxy_credentials: true,
      metadata_downloader_id: null,
      enabled: true,
      generation: 4,
      status: "untested",
      last_error: null,
      last_success_at: null as string | null,
      route: "required-proxy",
    },
  };

  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) return route.fallback();
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
    else if (path === "/api/sources/prowlarr/connection")
      data = connections.prowlarr;
    else if (path === "/api/sources/slskd/connection") data = connections.slskd;
    else if (path === "/api/sources/audiobookbay/connection")
      data = connections.audiobookbay;
    else if (path === "/api/sources/prowlarr/connection/test") {
      connections.prowlarr.status = "connected";
      connections.prowlarr.last_success_at = checkedAt;
      data = connections.prowlarr;
    } else if (path === "/api/sources/slskd/connection/test") {
      connections.slskd.status = "connected";
      connections.slskd.last_success_at = checkedAt;
      data = connections.slskd;
    } else if (path === "/api/sources/audiobookbay/connection/test") {
      connections.audiobookbay.status = "connected";
      connections.audiobookbay.last_success_at = checkedAt;
      data = connections.audiobookbay;
    } else if (path === "/api/sources/mam/connection")
      data = {
        configured: false,
        enabled: false,
        base_url: "https://www.myanonamouse.net",
        proxy_url: null,
        proxy_fallback_direct: true,
        has_session: false,
        has_proxy_credentials: false,
        generation: 0,
        status: "not-configured",
        last_error: null,
        last_success_at: null,
        route: "direct",
        automation: {},
      };
    return route.fulfill({ json: data });
  });

  await page.goto("/settings#sources");

  for (const source of [
    {
      region: "Prowlarr settings",
      form: "Prowlarr connection settings",
      secret: "API key",
    },
    {
      region: "Soulseek settings",
      form: "Soulseek connection settings",
      secret: "API key",
    },
  ]) {
    await page
      .getByRole("region", { name: source.region })
      .locator("summary")
      .first()
      .click();
    const form = page.getByRole("form", { name: source.form });
    await expect(
      form.getByLabel(source.secret, { exact: true }),
    ).toHaveAttribute("placeholder", "••••••••");
    await expect(form.getByLabel("Credential saved")).toContainText(
      "Saved · ••••••••",
    );
    const secret = form.getByLabel(source.secret, { exact: true });
    await secret.fill("replacement-api-key-value");
    await form
      .getByRole("button", {
        name:
          source.region === "Soulseek settings"
            ? "Save & test connection"
            : "Save connection",
        exact: true,
      })
      .click();
    await expect(secret).toHaveValue("");
    await expect(secret).toHaveAttribute("placeholder", "••••••••");
    if (source.region !== "Soulseek settings") {
      await expect(
        form.getByRole("status", { name: "Connection test status" }),
      ).toContainText("Saved · not tested");
      await form
        .getByRole("button", { name: "Test connection", exact: true })
        .click();
    }
    await expect(
      form.getByRole("status", { name: "Connection test status" }),
    ).toContainText("Saved & connected");
    await expect(
      form.getByRole("status", { name: "Connection test status" }),
    ).toContainText("Verified");
  }

  await page
    .getByRole("region", { name: "AudiobookBay settings" })
    .locator("summary")
    .first()
    .click();
  const abb = page.getByRole("form", {
    name: "AudiobookBay connection settings",
  });
  await abb.getByText("Proxy routing", { exact: true }).click();
  await expect(
    abb.getByLabel("Proxy password", { exact: true }),
  ).toHaveAttribute("placeholder", "••••••••");
  await expect(abb.getByLabel("Credential saved")).toContainText(
    "Saved · ••••••••",
  );
  await abb
    .getByRole("button", { name: "Test connection", exact: true })
    .click();
  await expect(
    abb.getByRole("status", { name: "Connection test status" }),
  ).toContainText("Saved & connected");
});
