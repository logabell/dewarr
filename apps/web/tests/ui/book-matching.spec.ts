import { test, expect } from "../fixtures";

test("book matching shows choices and keeps search in metadata until saved", async ({
  page,
}) => {
  const id = "00000000-0000-0000-0000-000000000042";
  const book = {
    provider: "hardcover",
    external_id: "42",
    title: "'Salem's Lot",
    authors: ["Stephen King"],
    publication_year: 1975,
    cover_url: "https://covers.example.test/portrait.svg",
    description:
      "A writer returns to a small Maine town and discovers an ancient evil.",
    editions: [],
    series: [],
    subjects: [],
  };
  let linked = false;
  let searches = 0;
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("https://covers.example.test/portrait.svg", (route) =>
    route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600"><rect width="400" height="600" fill="#897baa"/></svg>',
    }),
  );
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith("/api/")) return route.continue();
    let data: unknown = { items: [], total: 0 };
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "admin",
          role: "admin",
          username: "admin",
          display_name: "Reader",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === `/api/catalog/works/${id}`)
      data = {
        id,
        ...book,
        provisional: !linked,
        availability: {
          owned: true,
          audio: true,
          ebook: false,
          stale: false,
          in_collection: false,
        },
      };
    else if (path === `/api/metadata/works/${id}`)
      data = {
        sources: linked
          ? [
              {
                id: "source",
                provider: "hardcover",
                external_id: "42",
                title: book.title,
                book,
                series: [],
                fetched_at: "2026-09-20T00:00:00Z",
                editions_more: false,
              },
            ]
          : [],
        fields: {},
        versions: [],
        versions_total: 0,
        cover_choices: [],
        enrichment: null,
      };
    else if (path.endsWith("/reader-match"))
      data = {
        status: "unmatched",
        book: null,
        candidates: [
          book,
          { ...book, external_id: "43", publication_year: 2000 },
        ],
        reason: "More than one record fits.",
      };
    else if (path === "/api/metadata/search") {
      searches++;
      data = {
        provider: "hardcover",
        items: [book],
        known_works: {},
        page: 1,
        has_more: false,
      };
    } else if (path === "/api/metadata/books/hardcover/42") data = { book };
    else if (path.endsWith("/source") && route.request().method() === "POST") {
      linked = true;
      data = { id };
    } else if (path.endsWith("/reader-details"))
      data = {
        book_id: "42",
        authors: [],
        reviews: [],
        reviews_has_more: false,
        lists: [],
        tags: [],
      };
    else if (path.includes("/acquisition/preferences/"))
      data = { effective: { desired_media: "both" } };
    else if (path.includes("quick-add/latest")) data = null;
    else if (path.endsWith("/version-reviews")) data = [];
    else if (path.endsWith("/cover"))
      return route.fulfill({
        contentType: "image/svg+xml",
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600"><rect width="600" height="600" fill="#123456"/></svg>',
      });
    await route.fulfill({ json: data });
  });
  await page.goto(`/books/${id}?tab=manage`);
  await expect(
    page.getByRole("heading", { name: "Choose the right book" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Compare this book" }),
  ).toHaveCount(2);
  await expect(
    page.getByText("Metadata sources and protected edits"),
  ).not.toBeVisible();
  await expect(page.locator(".reader-hero .book-cover img")).toBeVisible();
  await page.screenshot({
    path: "test-results/book-match-desktop.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Search books", exact: true }).click();
  await expect(page.getByLabel("Title, author or identifier")).toHaveValue(
    "'Salem's Lot Stephen King",
  );
  await page
    .getByLabel("Title, author or identifier")
    .fill("Salem Stephen King");
  await page.getByRole("button", { name: "Search books", exact: true }).click();
  await expect(page).toHaveURL(/tab=manage/);
  await expect.poll(() => searches).toBeGreaterThanOrEqual(2);
  await page.getByRole("button", { name: /'Salem's Lot Stephen King/ }).click();
  await page
    .getByRole("button", { name: "Use this book", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Connected to Hardcover" }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "test-results/book-match-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});
