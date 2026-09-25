import { expect, test } from "../fixtures";

test("cover-led shelves, format overlays and persistent display preferences", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const art = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400"><rect width="400" height="400" fill="#b65732"/><circle cx="200" cy="150" r="85" fill="#eac68c"/><text x="200" y="300" text-anchor="middle" font-family="serif" font-size="34" fill="white">THE LONG WAY HOME</text></svg>')}`;
  const items = Array.from({ length: 12 }, (_, i) => ({
    book: {
      provider: "local",
      title:
        i === 0
          ? "The Long Way Home: A deliberately long title that should take only two lines"
          : `A reader’s journey ${i + 1}`,
      authors: ["Alex Morgan"],
      cover_url: art,
    },
    work: {
      id: `00000000-0000-0000-0000-${String(i).padStart(12, "0")}`,
      title:
        i === 0
          ? "The Long Way Home: A deliberately long title that should take only two lines"
          : `A reader’s journey ${i + 1}`,
      authors: ["Alex Morgan"],
      cover_url: art,
      availability: { owned: true, audio: true, ebook: i % 2 === 0 },
    },
    observed_at: "2026-09-19T00:00:00Z",
    reason: "Library copy first observed",
  }));
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    let data: unknown = { items: [], total: 0 };
    if (url.pathname === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "viewer",
          username: "reader",
          display_name: "Reader",
        },
        csrf_token: "test",
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "completed" };
    else if (url.pathname === "/api/metadata/account") data = { enabled: true };
    else if (url.pathname.includes("/cover"))
      return route.fulfill({ status: 404 });
    else if (url.pathname === "/api/discovery/library")
      data = {
        title: "Recent library additions",
        status: "ready",
        attribution: "Your library",
        items,
        has_more: true,
      };
    else if (url.pathname.startsWith("/api/discovery/hardcover"))
      data = {
        title: url.pathname.endsWith("trending")
          ? "Trending on Hardcover"
          : "New releases",
        status: "ready",
        items: items.slice(0, 5).map((item) => ({ ...item, work: null })),
        has_more: false,
      };
    else if (url.pathname.startsWith("/api/discovery/"))
      data = {
        title: "Your catalog picks",
        status: "ready",
        items: [],
        total: 0,
      };
    if (
      new URL(route.request().url()).pathname.includes(
        "/acquisition/preferences/",
      )
    )
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
  await page.goto("/discover");
  const shelf = page.getByRole("region", {
    name: "Recent library additions",
    exact: true,
  });
  await expect(shelf.locator(".book-card")).toHaveCount(12);
  await expect(shelf.locator(".cover-portrait")).toHaveCount(12);
  await expect(shelf.locator(".book-cover img").first()).toBeVisible();
  await expect(shelf.locator(".cover-owned")).toHaveCount(12);
  await expect(shelf.getByText("Library copy first observed")).toHaveCount(0);
  await expect(page.getByText("Library match not established")).toHaveCount(0);
  const heights = await shelf
    .locator(".book-card h3")
    .evaluateAll((nodes) =>
      nodes.map((node) => node.getBoundingClientRect().height),
    );
  expect(new Set(heights).size).toBe(1);
  await shelf.getByRole("button", { name: "Scroll library forward" }).click();
  await expect
    .poll(() =>
      shelf.locator(".discovery-shelf").evaluate((node) => node.scrollLeft),
    )
    .toBeGreaterThan(0);
  await shelf.getByLabel("Show library additions").selectOption("ebook");
  await expect(shelf.locator(".cover-portrait")).toHaveCount(12);
  await page.screenshot({
    path: testInfo.outputPath("discover-desktop.png"),
    fullPage: true,
  });
  await page.goto("/settings#display");
  const settings = page.getByRole("region", { name: "General", exact: true });
  await expect(settings.getByRole("checkbox")).toHaveCount(0);
  await settings
    .getByRole("group", { name: "Audiobook covers" })
    .getByLabel("Book portrait")
    .check();
  await settings
    .getByRole("group", { name: "Default covers", exact: true })
    .getByLabel("Square", { exact: true })
    .check();
  await page.reload();
  await expect(settings.getByRole("checkbox")).toHaveCount(0);
  await expect(
    settings
      .getByRole("group", { name: "Audiobook covers" })
      .getByLabel("Book portrait"),
  ).toBeChecked();
  await page.goto("/discover");
  await expect(shelf.locator(".cover-square")).toHaveCount(12);
  await shelf.getByLabel("Show library additions").selectOption("audio");
  await expect(shelf.locator(".cover-portrait")).toHaveCount(12);
  await expect(shelf.locator(".cover-formats")).toHaveCount(12);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("discover-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});
