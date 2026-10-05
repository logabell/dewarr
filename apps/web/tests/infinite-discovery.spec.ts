import { emptyDiscoveryHome, expect, test } from "./fixtures";

test.beforeEach(async ({ page }) => {
  const bootstrap = await page.request.post("/api/auth/bootstrap", {
    headers: { Origin: "http://127.0.0.1:8001" },
    data: {
      username: "reader",
      display_name: "Reader",
      password: "browser test password",
    },
  });
  const login =
    bootstrap.status() === 201
      ? bootstrap
      : await page.request.post("/api/auth/login", {
          headers: { Origin: "http://127.0.0.1:8001" },
          data: { username: "reader", password: "browser test password" },
        });
  expect(login.ok()).toBeTruthy();
  const auth = await login.json();
  await page.request.put("/api/setup/onboarding", {
    headers: {
      Origin: "http://127.0.0.1:8001",
      "X-CSRF-Token": auth.csrf_token,
    },
    data: { status: "completed", step: 0, skipped: [] },
  });
});

const entry = (n: number) => ({
  external_id: String(n),
  title: `Discovery book ${n}`,
  authors: ["Reader"],
  cover_url: null,
  winner: false,
  work: null,
});
const collection = (n: number) => ({
  id: `list-${n}`,
  kind: "listopia",
  title: `Collection ${n}`,
  source_url: "https://www.goodreads.com/list/show/1",
  genres: ["fantasy"],
  count: 41,
  coverage: "complete",
  updated_at: "2026-01-01T00:00:00Z",
  covers: [],
  saved: false,
  pinned: false,
  tracking: false,
});

test("collection sources share filters and awards append on scroll", async ({
  page,
}) => {
  await page.route("**/api/metadata/account", (route) =>
    route.fulfill({ json: { enabled: true } }),
  );
  await page.route("**/api/discovery/lists?*", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            external_id: "9",
            name: "Hardcover favorites",
            count: 10,
            covers: [],
          },
        ],
        has_more: false,
      },
    }),
  );
  const pages: number[] = [];
  await page.route("**/api/discovery/collections?*", (route) => {
    const query = new URL(route.request().url()).searchParams;
    const p = Number(query.get("page") || 1);
    pages.push(p);
    return route.fulfill({
      json: {
        items: Array.from({ length: p === 1 ? 24 : 1 }, (_, i) =>
          collection((p - 1) * 24 + i),
        ),
        total: 25,
        years: [2025],
        genres: ["fantasy"],
        categories: [],
        archive_gaps: [],
      },
    });
  });
  await page.goto("/discover?view=collections");
  await expect(page.getByLabel("Source", { exact: true })).toHaveValue("all");
  await expect(
    page.getByRole("heading", { name: "Collection 0", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Hardcover favorites" }),
  ).toHaveCount(1);
  await page.getByLabel("Source", { exact: true }).selectOption("hardcover");
  await expect(
    page.getByRole("heading", { name: "Collection 0", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "Hardcover favorites" }),
  ).toBeVisible();
  await page.getByLabel("Source", { exact: true }).selectOption("goodreads");
  await expect(
    page.getByRole("heading", { name: "Hardcover favorites" }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: "Awards", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "Stories worth celebrating",
      exact: true,
    }),
  ).toBeVisible();
  await page.locator(".explore-collection").nth(23).scrollIntoViewIfNeeded();
  await expect(page.locator(".explore-collection")).toHaveCount(25);
  expect(pages).toContain(2);
  await expect(
    page.getByRole("heading", { name: "Collection 0", exact: true }),
  ).toHaveCount(1);
});

test("browse keeps earlier books on next-page failure and resets with filters", async ({
  page,
}) => {
  let fail = true;
  await page.route("**/api/metadata/account", (route) =>
    route.fulfill({ json: { enabled: false } }),
  );
  await page.route("**/api/discovery/browse?*", (route) => {
    const params = new URL(route.request().url()).searchParams;
    const p = Number(params.get("page"));
    if (params.get("q"))
      return route.fulfill({
        json: { items: [entry(99)], total: 1, has_more: false, page: 1 },
      });
    if (p === 2 && fail)
      return route.fulfill({
        status: 503,
        json: { detail: "Temporarily unavailable" },
      });
    return route.fulfill({
      json: {
        items: Array.from({ length: p === 1 ? 40 : 1 }, (_, i) =>
          entry((p - 1) * 40 + i),
        ),
        total: 41,
        page: p,
        has_more: p === 1,
      },
    });
  });
  await page.goto("/discover?view=browse");
  await expect(page.locator(".explore-books > li")).toHaveCount(40);
  await page.locator(".infinite-scroll").scrollIntoViewIfNeeded();
  await expect(
    page.getByRole("button", { name: "Retry loading more" }),
  ).toBeVisible({ timeout: 15000 });
  await expect(page.locator(".explore-books > li")).toHaveCount(40);
  fail = false;
  await page.getByRole("button", { name: "Retry loading more" }).click();
  await expect(page.locator(".explore-books > li")).toHaveCount(41);
  await page
    .getByRole("textbox", { name: "Search discovery books" })
    .fill("filtered");
  await page.getByRole("button", { name: "Search discovery books" }).click();
  await expect(page.locator(".explore-books > li")).toHaveCount(1);
  await expect(
    page.getByRole("heading", { name: "Discovery book 99", exact: true }),
  ).toBeVisible();
});

test("collection download covers every page and format menu aligns", async ({
  page,
}, info) => {
  const ids: string[] = [];
  await page.route("**/api/metadata/account", (route) =>
    route.fulfill({ json: { enabled: false } }),
  );
  await page.route("**/api/discovery/collections/list-1?*", (route) => {
    const p = Number(
      new URL(route.request().url()).searchParams.get("page") || 1,
    );
    return route.fulfill({
      json: {
        collection: collection(1),
        items: Array.from({ length: p === 1 ? 40 : 1 }, (_, i) => ({
          ...entry((p - 1) * 40 + i),
          work: {
            id: `work-${(p - 1) * 40 + i}`,
            title: `Book ${i}`,
            authors: [],
            availability: { owned: false, ebook: false, audio: false },
          },
        })),
        total: 41,
        page: p,
        has_more: p === 1,
      },
    });
  });
  await page.route("**/api/requests/quick-add", (route) => {
    const body = route.request().postDataJSON();
    expect(body.specification.mode).toBe("ebook");
    ids.push(body.work_id);
    return route.fulfill({
      status: 202,
      json: { id: body.work_id, status: "queued", message: "Searching" },
    });
  });
  await page.goto("/discover/collections/list-1");
  await page.getByLabel("Download format for Collection 1").click();
  const both = await page
    .getByRole("button", { name: "Both", exact: true })
    .boundingBox();
  const ebook = await page
    .getByRole("button", { name: "Ebook", exact: true })
    .boundingBox();
  expect(Math.abs(both!.x - ebook!.x)).toBeLessThan(1);
  expect(ebook!.y).toBeGreaterThan(both!.y);
  await page.screenshot({ path: info.outputPath("collection-actions.png") });
  await page.getByRole("button", { name: "Ebook", exact: true }).click();
  await expect.poll(() => ids.length).toBe(41);
  expect(new Set(ids).size).toBe(41);
  await expect(
    page.getByRole("status").filter({ hasText: "41 queued" }),
  ).toBeVisible();
});

test("library appends books and changing format starts a fresh result set", async ({
  page,
}) => {
  const offsets: number[] = [];
  await page.route("**/api/library/books?*", (route) => {
    const params = new URL(route.request().url()).searchParams;
    const offset = Number(params.get("offset"));
    offsets.push(offset);
    const audio = params.get("medium") === "audio";
    return route.fulfill({
      json: {
        items: Array.from({ length: audio || offset ? 1 : 40 }, (_, i) => ({
          id: `book-${offset + i}`,
          title: `${audio ? "Audio" : "Library"} title ${offset + i}`,
          authors: [],
          cover_url: null,
          availability: { owned: true, ebook: !audio, audio },
        })),
        total: audio ? 1 : 41,
      },
    });
  });
  await page.goto("/library");
  const books = page.getByRole("region", {
    name: "Library books",
    exact: true,
  });
  await expect(books.locator(".book-card")).toHaveCount(40);
  await books.locator(".infinite-scroll").scrollIntoViewIfNeeded();
  await expect(books.locator(".book-card")).toHaveCount(41);
  expect(offsets).toContain(40);
  await page
    .getByRole("combobox", { name: "Media", exact: true })
    .selectOption("audio");
  await expect(books.locator(".book-card")).toHaveCount(1);
  await expect(
    page.getByRole("heading", { name: "Audio title 0" }),
  ).toBeVisible();
});

test("followed shelves append at the horizontal end without replacing books", async ({
  page,
}) => {
  const id = "00000000-0000-4000-8000-000000000123";
  const list = {
    id,
    name: "Reading shelf",
    count: 17,
    editable: false,
    shared: false,
  };
  await page.route("**/api/lists/page?*", (route) =>
    route.fulfill({ json: { items: [list], total: 1 } }),
  );
  await page.route(`**/api/lists/${id}?*`, (route) => {
    const offset = Number(
      new URL(route.request().url()).searchParams.get("offset"),
    );
    return route.fulfill({
      json: {
        ...list,
        matched: 17,
        content_revision: "revision",
        items: Array.from({ length: offset ? 1 : 16 }, (_, i) => ({
          id: `shelf-${offset + i}`,
          title: `Shelf title ${offset + i}`,
          authors: [],
          cover_url: null,
          provisional: false,
          availability: { owned: false, ebook: false, audio: false },
        })),
      },
    });
  });
  await page.route("**/api/metadata/account", (route) =>
    route.fulfill({ json: { enabled: false } }),
  );
  await page.goto("/discover?view=yours");
  const shelf = page.getByRole("list", { name: "Reading shelf book preview" });
  await expect(shelf.locator("li")).toHaveCount(16);
  await shelf.evaluate((el) => {
    el.scrollLeft = el.scrollWidth;
  });
  await expect(shelf.locator("li")).toHaveCount(18);
  await expect(shelf.locator(".shelf-view-all")).toHaveCount(1);
  await expect(
    shelf.getByRole("heading", { name: "Shelf title 0", exact: true }),
  ).toHaveCount(1);
  expect(await shelf.evaluate((el) => el.scrollLeft)).toBeGreaterThan(0);
});

test("Discover previews stop at 100 and View all continues scrolling", async ({
  page,
}, info) => {
  const c = {
    ...collection(1),
    count: 140,
    coverage: "partial",
    pinned: true,
    saved: true,
  };
  await page.route("**/api/metadata/account", (route) =>
    route.fulfill({ json: { enabled: false } }),
  );
  await page.route("**/api/lists/page?*", (route) =>
    route.fulfill({ json: { items: [], total: 0 } }),
  );
  await page.route("**/api/discovery/collections?*", (route) =>
    route.fulfill({
      json: {
        items: [c],
        total: 1,
        years: [],
        genres: [],
        categories: [],
        archive_gaps: [],
      },
    }),
  );
  await page.route("**/api/discovery/home", (route) => {
    const home = emptyDiscoveryHome();
    return route.fulfill({
      json: {
        ...home,
        saved: { ...home.saved, items: [c], total: 1 },
        selected: [c],
        layout: { order: [c.id], hidden: [] },
      },
    });
  });
  await page.route("**/api/discovery/collections/list-1?*", (route) => {
    const params = new URL(route.request().url()).searchParams;
    const p = Number(params.get("page") || 1);
    const total = params.get("full") === "true" ? 140 : 100;
    return route.fulfill({
      json: {
        collection: c,
        items: Array.from(
          { length: Math.min(40, total - (p - 1) * 40) },
          (_, i) => entry((p - 1) * 40 + i),
        ),
        total,
        page: p,
        has_more: p * 40 < total,
      },
    });
  });
  await page.goto("/discover");
  const row = page.getByRole("region", { name: "Collection 1", exact: true });
  const shelf = row.locator(".discovery-shelf");
  await expect(shelf.locator("li")).toHaveCount(40);
  await expect(
    row.getByRole("button", { name: "Download all missing", exact: true }),
  ).toBeVisible();
  for (const count of [80, 101]) {
    await shelf.evaluate((el) => {
      el.scrollLeft = el.scrollWidth;
    });
    await expect(shelf.locator("li")).toHaveCount(count);
  }
  const action = await row
    .getByRole("link", { name: "View all", exact: true })
    .boundingBox();
  const arrow = await row
    .getByRole("button", { name: "Scroll Collection 1 forward" })
    .boundingBox();
  const download = await row.locator(".quick-add-split").boundingBox();
  expect(action!.height).toBe(30);
  expect(download!.height).toBe(arrow!.height);
  await shelf.locator(".shelf-view-all").scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("discover-compact.png") });
  await shelf.locator(".shelf-view-all").click();
  await expect(page.locator(".explore-books > li")).toHaveCount(40);
  for (const count of [80, 120, 140]) {
    await page.locator(".infinite-scroll").scrollIntoViewIfNeeded();
    await expect(page.locator(".explore-books > li")).toHaveCount(count);
  }
  const source = page.getByRole("link", {
    name: "Open collection on Goodreads",
  });
  await expect(source).toHaveAttribute("href", c.source_url);
  await expect(source.locator("svg")).toHaveCount(1);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel("Download format for Collection 1").click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({ path: info.outputPath("collection-mobile.png") });
});

test("full personal list appends beyond 100 books", async ({ page }) => {
  const id = "00000000-0000-4000-8000-000000000123";
  await page.route(`**/api/lists/${id}?*`, (route) => {
    const offset = Number(
      new URL(route.request().url()).searchParams.get("offset") || 0,
    );
    return route.fulfill({
      json: {
        id,
        name: "Long reading list",
        count: 121,
        editable: true,
        items: Array.from({ length: Math.min(40, 121 - offset) }, (_, i) => ({
          id: `work-${offset + i}`,
          title: `Book ${offset + i}`,
          authors: [],
          availability: { owned: false, ebook: false, audio: false },
        })),
      },
    });
  });
  await page.goto(`/discover?view=yours&list=${id}`);
  await expect(page.locator(".explore-books > li")).toHaveCount(40);
  for (const count of [80, 120, 121]) {
    await page.locator(".infinite-scroll").scrollIntoViewIfNeeded();
    await expect(page.locator(".explore-books > li")).toHaveCount(count);
  }
  await expect(
    page.getByRole("heading", { name: "Book 0", exact: true }),
  ).toHaveCount(1);
  await expect(
    page.getByRole("heading", { name: "Book 120", exact: true }),
  ).toBeVisible();
});

test("full Goodreads lists retain books on upstream failure and retry past 100", async ({
  page,
}) => {
  let fail = true;
  const c = { ...collection(1), count: 3800, coverage: "partial" };
  await page.route("**/api/discovery/collections/list-1?*", (route) => {
    const params = new URL(route.request().url()).searchParams;
    const p = Number(params.get("page") || 1);
    if (p > 1) expect(params.get("full")).toBe("true");
    if (p === 3 && fail)
      return route.fulfill({
        status: 502,
        json: { detail: "Goodreads could not load more books. Try again." },
      });
    return route.fulfill({
      json: {
        collection: c,
        items: Array.from({ length: 40 }, (_, i) => entry((p - 1) * 40 + i)),
        total: 3800,
        page: p,
        has_more: true,
      },
    });
  });
  await page.goto("/discover/collections/list-1");
  await expect(page.locator(".explore-books > li")).toHaveCount(40);
  await expect(page.getByText("3,800 books", { exact: false })).toBeVisible();
  await expect(
    page.getByText("The remaining books are available on Goodreads", {
      exact: false,
    }),
  ).toHaveCount(0);
  await page.locator(".infinite-scroll").scrollIntoViewIfNeeded();
  await expect(page.locator(".explore-books > li")).toHaveCount(80);
  await page.locator(".infinite-scroll").scrollIntoViewIfNeeded();
  await expect(
    page.getByRole("button", { name: "Retry loading more" }),
  ).toBeVisible();
  await expect(page.locator(".explore-books > li")).toHaveCount(80);
  fail = false;
  await page.getByRole("button", { name: "Retry loading more" }).click();
  await expect(page.locator(".explore-books > li")).toHaveCount(120);
  await expect(
    page.getByRole("heading", { name: "Discovery book 0", exact: true }),
  ).toHaveCount(1);
});
