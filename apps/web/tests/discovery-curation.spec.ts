import { expect, test } from "./fixtures";

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

test("awards, filtering, pinning and persisted layout form one discovery flow", async ({
  page,
}, info) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/discover");
  await expect(
    page.getByRole("heading", { name: "Discover", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("navigation", { name: "Discover navigation" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "View My Friends", exact: true }),
  ).toBeVisible();
  const nav = await page.locator(".page-tabs").boundingBox();
  expect(nav!.height).toBeLessThan(70);
  const covers = page
    .getByRole("region", { name: "Fiction", exact: true })
    .locator(".book-cover");
  const first = await covers.nth(0).boundingBox();
  const second = await covers.nth(1).boundingBox();
  expect(Math.abs(first!.y - second!.y)).toBeLessThan(2);
  await page.screenshot({
    path: info.outputPath("discover-desktop.png"),
    fullPage: true,
  });
  await page.getByRole("link", { name: "Awards", exact: true }).click();
  await page
    .getByRole("combobox", { name: "Year", exact: true })
    .selectOption("2025");
  await page
    .getByRole("combobox", { name: "Category", exact: true })
    .selectOption("Fiction");
  await expect(page.locator(".explore-collection")).toHaveCount(1);
  await page.locator(".explore-collection").click();
  await expect(
    page.getByRole("link", { name: "View My Friends", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Winner", exact: true }).click();
  await expect(page.locator(".explore-books > li")).toHaveCount(1);
  await page
    .getByRole("button", { name: "Show on For you", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "On For you", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Tracking on", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Tracking paused", exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("button", { name: "On For you", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Tracking paused", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: "For you", exact: true }).click();
  await page.getByRole("button", { name: "Customize", exact: true }).click();
  await page.getByLabel("2025 Fiction nominees", { exact: true }).uncheck();
  await page.getByRole("button", { name: "Save layout", exact: true }).click();
  await page.reload();
  await expect(
    page.getByRole("region", { name: "Fiction", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: "Browse", exact: true }).click();
  await page
    .getByRole("textbox", { name: "Search discovery books", exact: true })
    .fill("Wild Dark Shore");
  await page
    .getByRole("button", { name: "Search discovery books", exact: true })
    .click();
  await expect(
    page.getByRole("link", { name: "View Wild Dark Shore", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "View Wild Dark Shore", exact: true })
    .click();
  await expect(page).toHaveURL(/\/discover\/books\/goodreads\/\d+/);
  await expect(
    page.getByRole("heading", { name: "Wild Dark Shore", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Connect Hardcover", exact: true }),
  ).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("manual list preview is bounded and mobile navigation fits", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/discover");
  await page.getByRole("button", { name: "Add list", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Add a list" });
  await dialog
    .getByLabel("List URL")
    .fill(
      "https://www.goodreads.com/list/show/50.The_Best_Epic_Fantasy_fiction_",
    );
  await dialog.getByRole("button", { name: "Preview", exact: true }).click();
  await expect(
    dialog.getByRole("heading", {
      name: "The Best Epic Fantasy (fiction)",
      exact: true,
    }),
  ).toBeVisible();
  await dialog.getByLabel("Keep updated").uncheck();
  await dialog.getByRole("button", { name: "Add list", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Tracking paused", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: info.outputPath("collection-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("link", { name: "Awards", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Goodreads Choice Awards", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: info.outputPath("awards-mobile.png"),
    fullPage: true,
  });
});

test("Goodreads cards open verified Hardcover details and lists navigation is consolidated", async ({
  page,
}) => {
  await page.route("https://**/*.{jpg,png}", (route) =>
    route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300"><rect width="200" height="300"/></svg>',
    }),
  );
  const book = {
    provider: "hardcover",
    external_id: "42",
    title: "Ender's Game",
    authors: ["Orson Scott Card"],
    cover_url: "https://assets.hardcover.app/enders-game.jpg",
    description: "Verified Hardcover book description.",
    editions: [],
    series: [],
    subjects: [],
    editions_more: false,
  };
  await page.route("**/api/discovery/goodreads/375802", (route) =>
    route.fulfill({
      json: {
        entry: {
          external_id: "375802",
          title: "Ender’s Game (Ender's Saga, #1)",
          authors: book.authors,
          work: null,
        },
        match: {
          status: "matched",
          book,
          candidates: [],
          reason: "Verified title and author",
        },
      },
    }),
  );
  await page.route(/\/api\/metadata\/books\/hardcover\/42$/, (route) =>
    route.fulfill({ json: { book, work: null, stale: false } }),
  );
  await page.goto("/discover/collections/gr-list-19341");
  await expect(
    page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: "Lists", exact: true }),
  ).toHaveCount(0);
  await expect(
    page
      .getByRole("navigation", { name: "Discover navigation" })
      .getByRole("link", { name: "Your lists", exact: true }),
  ).toBeVisible();
  const link = page.getByRole("link", {
    name: "View Ender’s Game (Ender's Saga, #1)",
    exact: true,
  });
  await expect(link.locator("..").locator(".book-cover img")).toBeVisible();
  await link.click();
  await expect(page).toHaveURL(/\/discover\/books\/hardcover\/42$/);
  await expect(
    page.getByRole("heading", { name: "Ender's Game", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Quick add", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Verified Hardcover book description.", { exact: true }),
  ).toBeVisible();
});

test("visible Goodreads cards replace thumbnails with Hardcover art", async ({
  page,
}) => {
  await page.route("**/api/metadata/account", (route) =>
    route.fulfill({ json: { enabled: true } }),
  );
  await page.route("**/api/discovery/goodreads/*", (route) => {
    const id = route.request().url().split("/").pop();
    const match =
      id === "375802"
        ? {
            external_id: "42",
            provider: "hardcover",
            title: "Ender's Game",
            authors: ["Orson Scott Card"],
            cover_url: "https://assets.hardcover.app/discovery-test-cover.svg",
          }
        : null;
    return route.fulfill({
      json: {
        entry: { external_id: id, work: null },
        match: {
          book: match,
          candidates: [],
          status: match ? "matched" : "unmatched",
        },
      },
    });
  });
  await page.route(
    "https://assets.hardcover.app/discovery-test-cover.svg",
    (route) =>
      route.fulfill({
        contentType: "image/svg+xml",
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600"><rect width="400" height="600" fill="#456"/></svg>',
      }),
  );
  await page.goto("/discover/collections/gr-list-19341");
  const card = page.getByRole("link", {
    name: "View Ender’s Game (Ender's Saga, #1)",
    exact: true,
  });
  await expect(card).toHaveAttribute("href", "/discover/books/hardcover/42");
  await expect(card.locator("..").locator(".book-cover img")).toHaveAttribute(
    "src",
    "/api/catalog/cover-image?url=" +
      encodeURIComponent(
        "https://assets.hardcover.app/discovery-test-cover.svg",
      ),
  );
});
