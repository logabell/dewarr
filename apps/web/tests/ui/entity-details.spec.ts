import { expect, test, type Page } from "../fixtures";

const author = {
  external_id: "9",
  name: "Ursula K. Le Guin",
  slug: "ursula-k-le-guin",
  bio: "Ursula K. Le Guin explored imagined worlds with a deep curiosity about language, identity, and the ways people live together. Her Earthsea novels follow a journey across an archipelago of islands, where knowing a thing’s true name gives it power.",
  image_url: null,
};
const titles = [
  "A Wizard of Earthsea",
  "The Tombs of Atuan",
  "The Farthest Shore",
];
const books = titles.map((title, i) => ({
  provider: "hardcover",
  external_id: String(101 + i),
  title,
  authors: [author.name],
  publication_year: 1968 + i * 2,
  cover_url: `/fixture-cover-${i}.svg`,
}));
const works = books.map((book, i) => ({
  ...book,
  id: `work-${i}`,
  availability: { owned: i === 0, ebook: i === 0, audio: false, stale: false },
}));
async function fixtures(page: Page, role = "viewer") {
  await page.route("**/fixture-cover-*.svg", (route) =>
    route.fulfill({
      contentType: "image/svg+xml",
      body: `<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450"><rect width="300" height="450" fill="#264c50"/><circle cx="150" cy="200" r="86" fill="#c7ac73"/><text x="150" y="55" text-anchor="middle" fill="#eee0c3" font-family="serif" font-size="24">EARTHSEA</text><path d="M30 400L150 120L270 400Z" fill="#193a40"/></svg>`,
    }),
  );
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    let data: unknown = {};
    if (url.pathname === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role,
          username: "reader",
          display_name: "Reader",
        },
        csrf_token: "test",
      };
    else if (url.pathname === "/api/catalog/works")
      data = { items: [], total: 0 };
    else if (url.pathname === "/api/metadata/search")
      data = {
        items: [books[0]],
        provider: "hardcover",
        known_works: {},
        has_more: false,
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "completed" };
    else if (url.pathname === "/api/metadata/authors/hardcover/9")
      data = {
        author,
        books: url.searchParams.get("page") === "2" ? [] : books,
        page: Number(url.searchParams.get("page") || 1),
        has_more: url.searchParams.get("page") !== "2",
        known_works: {},
      };
    else if (url.pathname === "/api/metadata/books/hardcover/101")
      data = {
        book: {
          ...books[0],
          description: "A young wizard begins his journey.",
          series: [{ external_id: "7", name: "Earthsea", position: "1" }],
        },
        work: null,
      };
    else if (url.pathname.endsWith("/reader-details"))
      data = { external_id: "101", authors: [author], reviews: [] };
    else if (url.pathname === "/api/catalog/series/hardcover/7")
      data = {
        external_id: "7",
        name: "Earthsea",
        description:
          "An archipelago of islands, dragons, and true names. Follow Ged’s journey through the world of Earthsea.",
        status: "succeeded",
        message: "",
        fetched_at: "2026-09-19T12:00:00Z",
        generation: 1,
        books: 3,
        owned: 1,
        ebook: 1,
        audio: 0,
        total: 3,
        items: works.map((work, i) => ({
          membership_id: `member-${i}`,
          work,
          position: String(i + 1),
          publication: "published",
          compilation: false,
          partial: false,
          merged_record: false,
          ambiguous_position: false,
        })),
      };
    else if (url.pathname === "/api/lists/page") data = { items: [], total: 0 };
    else if (url.pathname.endsWith("/requests/saved-request"))
      return route.fulfill({
        status: 404,
        json: { detail: "Request no longer available" },
      });
    else if (url.pathname.endsWith("/main-books")) data = null;
    else if (url.pathname.endsWith("/requests"))
      data = { items: [], total: 0, offset: 0, limit: 25 };
    else if (
      url.pathname === "/api/lists" ||
      url.pathname === "/api/acquisition/profiles" ||
      url.pathname === "/api/library/libraries"
    )
      data = [];
    if (
      new URL(route.request().url()).pathname.includes(
        "/acquisition/preferences/",
      )
    )
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
}

test("author links stay in app; biography tabs, pagination, and mobile work", async ({
  page,
}, testInfo) => {
  await fixtures(page);
  const authorPages: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/metadata/authors/"))
      authorPages.push(request.url());
  });
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/discover/books/hardcover/101");
  const authorLink = page
    .locator(".author-line")
    .getByRole("link", { name: author.name });
  await expect(authorLink).toHaveAttribute("href", "/authors/hardcover/9");
  await page
    .locator(".author-line")
    .getByRole("link", { name: author.name })
    .click();
  await expect(page).toHaveURL(/\/authors\/hardcover\/9$/);
  await expect(
    page.getByRole("heading", { name: author.name, exact: true }),
  ).toBeVisible();
  await expect(page.locator(".entity-book-grid .book-card")).toHaveCount(3);
  await expect(
    page.getByRole("link", { name: "View A Wizard of Earthsea" }),
  ).toHaveAttribute("href", "/discover/books/hardcover/101");
  await page.screenshot({
    path: testInfo.outputPath("author-desktop.png"),
    fullPage: true,
  });
  await page.getByRole("tab", { name: "Books", exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(
    page.getByRole("tab", { name: "About the author" }),
  ).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("tabpanel")).toContainText(author.bio);
  await page.reload();
  await expect(
    page.getByRole("tab", { name: "About the author" }),
  ).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "Books", exact: true }).click();
  await page.locator(".entity-book-grid").scrollIntoViewIfNeeded();
  await expect
    .poll(() =>
      authorPages.some((url) => new URL(url).searchParams.get("page") === "2"),
    )
    .toBe(true);
  await expect(page.locator(".entity-book-grid .book-card")).toHaveCount(3);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator(".entity-book-grid .book-card")).toHaveCount(3);
  await page.screenshot({
    path: testInfo.outputPath("author-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});

test("series uses reading order and library counts with accessible tabs", async ({
  page,
}, testInfo) => {
  await fixtures(page);
  await page.goto("/series/hardcover/7");
  await expect(
    page.getByRole("heading", { name: "Earthsea", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("meter")).toHaveAttribute("aria-valuenow", "1");
  await expect(page.locator(".series-book-row")).toHaveCount(3);
  await expect(page.locator(".series-book-row").first()).toContainText(
    "In library",
  );
  await expect(page.getByRole("tab", { name: "Lists & requests" })).toHaveCount(
    0,
  );
  await expect(
    page.locator(".series-row-copy").first().getByRole("link"),
  ).toHaveAttribute("href", "/books/work-0");
  await page.screenshot({
    path: testInfo.outputPath("series-desktop.png"),
    fullPage: true,
  });
  await page.getByRole("tab", { name: "About the series" }).click();
  await expect(page.getByRole("tabpanel")).toContainText("An archipelago");
  await page.goBack();
  await expect(
    page.getByRole("tab", { name: "Reading order" }),
  ).toHaveAttribute("aria-selected", "true");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("series-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("author unavailable state supports retry", async ({ page }) => {
  await fixtures(page);
  await page.route("**/api/metadata/authors/hardcover/9?*", (route) =>
    route.fulfill({ status: 503, json: { detail: "Catalog unavailable" } }),
  );
  await page.goto("/authors/hardcover/9");
  await expect(
    page.getByRole("heading", { name: "Author details unavailable" }),
  ).toBeVisible();
  await page.unroute("**/api/metadata/authors/hardcover/9?*");
  await page.getByRole("button", { name: "Try again" }).click();
  await expect(
    page.getByRole("heading", { name: author.name, exact: true }),
  ).toBeVisible();
});

test("series selections survive browsing tabs and requests are separate", async ({
  page,
}) => {
  await fixtures(page, "admin");
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/series/hardcover/7");
  await expect(page.getByRole("checkbox")).toHaveCount(0);
  await page.getByRole("tab", { name: "Lists & requests" }).click();
  await page.getByLabel("Select A Wizard of Earthsea", { exact: true }).check();
  await expect(
    page.getByRole("region", { name: "Series requests", exact: true }),
  ).toContainText("1 books selected");
  await page.getByRole("tab", { name: "About the series" }).click();
  await page.getByRole("tab", { name: "Lists & requests" }).click();
  await expect(
    page.getByLabel("Select A Wizard of Earthsea", { exact: true }),
  ).toBeChecked();
  await page.reload();
  await expect(
    page.getByRole("tab", { name: "Lists & requests" }),
  ).toHaveAttribute("aria-selected", "true");
  await page.goto("/series/hardcover/7?request=saved-request");
  await expect(
    page.getByRole("tab", { name: "Lists & requests" }),
  ).toHaveAttribute("aria-selected", "true");
  expect(errors).toEqual([]);
});

test("global search opens book details and legacy source menus return to search", async ({
  page,
}) => {
  await fixtures(page, "member");
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/") && request.method() !== "GET")
      writes.push(request.url());
  });
  await page.goto("/search");
  const navigation = page.getByRole("navigation", { name: "Main navigation" });
  await expect(
    navigation.getByRole("link", { name: "Search books", exact: true }),
  ).toHaveCount(0);
  await expect(
    navigation.getByRole("link", { name: "Sources", exact: true }),
  ).toHaveCount(0);
  const search = page.getByRole("textbox", { name: "Search books or authors" });
  await search.fill("Earthsea");
  await search.press("Enter");
  await page.getByRole("link", { name: "View A Wizard of Earthsea" }).click();
  await expect(page).toHaveURL(/\/discover\/books\/hardcover\/101$/);
  await expect(
    page.getByRole("heading", { name: titles[0], exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Search sources", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Back to search results" }).click();
  await expect(search).toHaveValue("Earthsea");
  for (const path of [
    "/sources",
    "/sources/audiobookbay",
    "/sources/prowlarr",
  ]) {
    await page.goto(`${path}?q=Earthsea`);
    await expect(page).toHaveURL(/\/search\?q=Earthsea$/);
    await expect(search).toHaveValue("Earthsea");
  }
  expect(writes).toEqual([]);
});
