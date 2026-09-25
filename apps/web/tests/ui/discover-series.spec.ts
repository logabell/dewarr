import { expect, test } from "../fixtures";

const series = (n: number) => ({
  external_id: String(n),
  name: `The Coast Chronicles ${n}`,
  fetched_at: "2026-09-20T00:00:00Z",
  catalog_stale: false,
  inventory_stale: false,
  owned: 2,
  ebook: 2,
  audio: 0,
  published: 6,
  missing: 4,
  unknown_publication: 0,
  future_publication: 1,
  unseen: 0,
  books: [1.5, 2, 10].map((position) => ({
    position: String(position),
    ambiguous_position: position === 2,
    unseen: false,
    work: {
      id: `book-${n}-${position}`,
      title:
        position === 1.5
          ? "Beyond the Tides"
          : position === 2
            ? "A Lantern at the Edge of the World"
            : "Return to the Far Shore",
      authors: ["Coastal Writer"],
      cover_url: `https://example.com/book-${n}-${position}.jpg`,
      publication_year: 2020,
      provisional: true,
      availability: { owned: false, ebook: false, audio: false, stale: false },
    },
  })),
});

test("Discover series tab scrolls through saved pages without starting catalog refreshes", async ({
  page,
}, info) => {
  const requests: string[] = [];
  const pages: string[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/catalog/cover-image") return route.fallback();
    requests.push(`${route.request().method()} ${url.pathname}`);
    let data: unknown = [];
    if (url.pathname === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "viewer",
          display_name: "Reader",
          onboarding_status: "complete",
          permissions: [],
        },
        csrf_token: "test",
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "completed" };
    else if (url.pathname === "/api/metadata/account")
      data = { enabled: false, suggest_series_gaps: true };
    else if (url.pathname === "/api/discovery/layout")
      data = { order: ["series"], hidden: [] };
    else if (url.pathname === "/api/discovery/collections")
      data = {
        items: [],
        total: 0,
        years: [],
        genres: [],
        categories: [],
        archive_gaps: [],
      };
    else if (url.pathname === "/api/lists/page") data = { items: [], total: 0 };
    else if (url.pathname === "/api/discovery/library")
      data = { items: [], has_more: false };
    else if (url.pathname === "/api/discovery/series") {
      const p = Number(url.searchParams.get("page") || 1);
      const medium = url.searchParams.get("medium") || "any";
      pages.push(`${medium}:${p}`);
      data = {
        items: Array.from({ length: p === 1 ? 6 : 1 }, (_, i) =>
          series((p - 1) * 6 + i + 1),
        ),
        page: p,
        medium,
        has_more: p === 1,
        suggestions_enabled: true,
        hardcover_connected: true,
        monitored_series: 7,
      };
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/discover");
  const nav = page.getByRole("navigation", { name: "Discover navigation" });
  await expect(nav).toBeVisible();
  await page.mouse.wheel(0, 3000);
  await expect(
    page.getByRole("heading", { name: "Missing from your series" }),
  ).toHaveCount(0);
  expect(pages).toEqual([]);
  await nav.getByRole("link", { name: "Your series" }).click();
  await expect(nav.getByRole("link", { name: "Your series" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(page.getByRole("article")).toHaveCount(6);
  await expect(
    page.getByText("Book 1.5", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByText("Book 2 · Order needs review", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    page.getByRole("img", { name: "Cover of Beyond the Tides" }).first(),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("series-desktop.png"),
    fullPage: true,
  });
  await page.locator(".infinite-scroll").scrollIntoViewIfNeeded();
  await expect(page.getByRole("article")).toHaveCount(7);
  expect(pages).toEqual(["any:1", "any:2"]);
  await nav.getByRole("link", { name: "For you" }).click();
  await nav.getByRole("link", { name: "Your series" }).click();
  await expect(page.getByRole("article")).toHaveCount(7);
  expect(pages).toEqual(["any:1", "any:2"]);
  await page.getByLabel("Find missing").selectOption("audio");
  await expect.poll(() => pages.includes("audio:1")).toBe(true);
  expect(requests.some((request) => request.endsWith("/refresh"))).toBe(false);
  expect(
    requests.some((request) => request.startsWith("POST /api/metadata")),
  ).toBe(false);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: info.outputPath("series-mobile.png"),
    fullPage: false,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.goto("/discover/series");
  await expect(page).toHaveURL(/\/discover\?view=series$/);
  await expect(nav.getByRole("link", { name: "Your series" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  expect(errors).toEqual([]);
});
