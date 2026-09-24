import { expect, test } from "./fixtures";

for (const explicitTest of [true, false]) {
  test(`Hardcover connects through ${explicitTest ? "testing" : "loading lists"} and carries to settings`, async ({
    page,
  }, testInfo) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let progress = { status: "pending", step: 0, skipped: [] };
    let account = {
      configured: false,
      enabled: false,
      status: "not-configured",
      last_error: null,
      last_success_at: null as string | null,
    };
    let tests = 0;
    let listRequests = 0;
    await page.route("**/api/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      let data: unknown = [];
      if (path === "/api/auth/me")
        data = {
          user: {
            id: "reader",
            role: "admin",
            display_name: "Reader",
            onboarding_status: progress.status,
            permissions: [],
          },
          csrf_token: "test",
        };
      else if (path === "/api/setup/onboarding") {
        if (route.request().method() === "PUT")
          progress = route.request().postDataJSON();
        data = progress;
      } else if (path === "/api/metadata/account") {
        if (route.request().method() === "PUT")
          account = {
            ...account,
            configured: true,
            enabled: true,
            status: account.configured ? account.status : "untested",
          };
        data = account;
      } else if (path === "/api/metadata/account/test") {
        tests++;
        account = {
          ...account,
          status: "connected",
          last_success_at: "2026-09-24T12:00:00Z",
        };
        data = account;
      } else if (path === "/api/metadata/preferences")
        data = { primary: "hardcover", covers: "hardcover", language: "en" };
      else if (path === "/api/metadata/hardcover-lists") {
        listRequests++;
        account = {
          ...account,
          status: "connected",
          last_success_at: "2026-09-24T12:00:00Z",
        };
        data = {
          items: [{ external_id: "42", name: "Autumn reading", count: 12 }],
          next_cursor: null,
        };
      } else if (
        path === "/api/reading-accounts/goodreads" ||
        path === "/api/reading-accounts/storygraph"
      )
        data = null;
      else if (path === "/api/lists/page")
        data = { items: [], total: 0, offset: 0, limit: 25 };
      else if (path.includes("/acquisition/preferences/"))
        data = { effective: { desired_media: "both" } };
      return route.fulfill({ json: data });
    });
    await page.goto("/onboarding");
    await page.getByLabel("Hardcover API token").fill("synthetic-token");
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    if (explicitTest) {
      await page
        .getByRole("button", { name: "Test connection", exact: true })
        .click();
      await expect(
        page.locator(".metadata-connection .connection-state"),
      ).toHaveText("Connected");
    } else {
      await expect(
        page.locator(".metadata-connection .connection-state"),
      ).toHaveText("Not tested");
    }
    // A repeat save must not turn a verified connection into an untested one.
    await page
      .getByRole("button", { name: "Save connection", exact: true })
      .click();
    await page
      .getByRole("navigation", { name: "Setup steps" })
      .getByRole("button", { name: /Reading accounts/ })
      .click();
    const hardcover = page.getByRole("region", {
      name: "Hardcover connection",
    });
    await expect(hardcover.locator(".connection-state")).toHaveAttribute(
      "data-state",
      "connected",
    );
    await expect(hardcover.locator(".connection-state svg")).toBeVisible();
    await expect(
      hardcover.getByRole("checkbox", { name: "Autumn reading 12 books" }),
    ).toBeVisible();
    expect(tests).toBe(explicitTest ? 1 : 0);
    expect(listRequests).toBeGreaterThan(0);

    for (const provider of ["Goodreads", "StoryGraph", "Hardcover"]) {
      const section = page.getByRole("region", {
        name: `${provider} connection`,
      });
      const heading = section
        .locator(".reading-account-heading > span")
        .first();
      const help = section.getByRole("button", {
        name: `About ${provider}${provider === "Hardcover" ? " lists" : ""}`,
        exact: true,
      });
      const labelBox = (await heading.boundingBox())!;
      const helpBox = (await help.boundingBox())!;
      expect(helpBox.x - labelBox.x - labelBox.width).toBeLessThan(12);
      await help.focus();
      await expect(section.getByRole("tooltip")).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(section.locator(".reading-connection")).toHaveAttribute(
        "open",
        "",
      );
    }
    const goodreads = page.getByRole("region", {
      name: "Goodreads connection",
    });
    const input = (await goodreads
      .getByLabel("Goodreads profile or books link")
      .boundingBox())!;
    const button = (await goodreads
      .getByRole("button", { name: "Connect Goodreads", exact: true })
      .boundingBox())!;
    expect(
      Math.abs(input.y + input.height - button.y - button.height),
    ).toBeLessThan(2);
    await expect(
      page.getByRole("link", { name: "Sign in to StoryGraph ↗", exact: true }),
    ).toBeVisible();
    await page.locator(".onboarding-card").screenshot({
      path: testInfo.outputPath("reading-onboarding-desktop.png"),
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await expect(
      hardcover.getByRole("checkbox", { name: "Autumn reading 12 books" }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.locator(".onboarding-card").screenshot({
      path: testInfo.outputPath("reading-onboarding-mobile.png"),
    });
    await page.reload();
    await expect(hardcover.locator(".connection-state")).toHaveText(
      "Connected",
    );
    progress.status = "completed";
    await page.goto("/settings#reading");
    await hardcover.locator(".reading-connection > summary").click();
    await expect(
      hardcover.getByRole("checkbox", { name: "Autumn reading 12 books" }),
    ).toBeVisible();
    await expect(hardcover.locator(".connection-state")).toHaveText(
      "Connected",
    );
    account = { ...account, enabled: false, status: "disabled" };
    await page.reload();
    await hardcover.locator(".reading-connection > summary").click();
    await expect(hardcover.locator(".connection-state")).toHaveText("Disabled");
    await expect(hardcover.locator(".connection-state svg")).toHaveCount(0);
    await expect(
      hardcover.getByRole("link", { name: "Set up Hardcover" }),
    ).toBeVisible();
    expect(errors).toEqual([]);
  });
}
