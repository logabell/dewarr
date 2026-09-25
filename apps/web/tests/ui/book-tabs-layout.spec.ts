import { expect, test } from "../fixtures";

test("editions paginate and reviews expand, sort and protect spoilers", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const editions = Array.from({ length: 45 }, (_, i) => ({
    external_id: String(i),
    title: `Edition ${i + 1}`,
    medium: i % 2 ? "audio" : "ebook",
    narrators: [],
    language: "en",
  }));
  const work = {
    id: "book",
    title: "Wild Dark Shore",
    authors: ["Charlotte McConaghy"],
    description: "A family at the edge of the world.",
    availability: { owned: true, ebook: true, audio: false, stale: false },
  };
  const longReview = "A beautifully atmospheric story. ".repeat(25);
  const offsets: number[] = [];
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    let data: unknown = { items: [], total: 0 };
    if (url.pathname === "/api/auth/me")
      data = {
        user: {
          id: "user",
          username: "reader",
          role: "member",
          display_name: "Reader",
        },
        csrf_token: "test",
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "completed" };
    else if (url.pathname.endsWith("/reader-details"))
      data = {
        external_id: "42",
        rating: 4.2,
        ratings_count: 355,
        pages: 305,
        authors: [],
        reviews: [
          {
            external_id: "1",
            username: "coastalreader",
            rating: 4,
            text: longReview,
            spoilers: false,
            reviewed_at: "2025-01-01T00:00:00Z",
          },
          {
            external_id: "2",
            username: "booklover",
            rating: 5,
            text: "The ending is a secret.",
            spoilers: true,
            reviewed_at: "2025-05-01T00:00:00Z",
          },
        ],
      };
    else if (url.pathname === "/api/metadata/books/hardcover/42")
      data = {
        book: {
          provider: "hardcover",
          external_id: "42",
          ...work,
          editions,
          subjects: [],
        },
        work: null,
      };
    else if (url.pathname === "/api/catalog/works/book") data = work;
    else if (url.pathname === "/api/metadata/works/book") {
      const offset = Number(url.searchParams.get("offset") || 0);
      offsets.push(offset);
      data = {
        sources: [
          { provider: "hardcover", external_id: "42", book: {}, series: [] },
        ],
        versions_total: 45,
        versions: editions.slice(offset, offset + 20).map((e) => ({
          ...e,
          id: e.external_id,
          owned: false,
          needs_review: false,
          identifiers: {},
        })),
        cover_choices: [],
        fields: {},
      };
    } else if (url.pathname.includes("/acquisition/preferences/"))
      data = { effective: { desired_media: "both" } };
    else if (url.pathname.includes("quick-add/latest")) data = null;
    await route.fulfill({ json: data });
  });
  await page.goto("/discover/books/hardcover/42?tab=editions");
  await expect(page.locator(".reader-edition")).toHaveCount(20);
  await page
    .getByRole("button", { name: "Next editions page", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Edition 21", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Next editions page", exact: true })
    .click();
  await expect(page.locator(".reader-edition")).toHaveCount(5);
  await expect(
    page.getByRole("button", { name: "Next editions page", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Edition format").selectOption("audio");
  await expect(
    page.getByText("1–20 of 22 editions", { exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "Reviews", exact: true }).click();
  await expect(
    page.getByText("The ending is a secret.", { exact: true }),
  ).not.toBeVisible();
  await page.getByRole("button", { name: "Read full review" }).click();
  await expect(
    page.locator(".reader-review").first().locator(".reader-prose"),
  ).toHaveText(longReview.trim());
  await page.getByRole("button", { name: "Show less" }).click();
  await page.getByLabel("Sort reviews").selectOption("recent");
  await expect(page.locator(".reader-review").first()).toContainText(
    "@booklover",
  );
  await page.getByText("Contains spoilers · Reveal review").click();
  await expect(
    page.getByText("The ending is a secret.", { exact: true }),
  ).toBeVisible();
  const actions = page.locator(".reader-actions");
  const add = await actions
    .getByRole("button", { name: "Add to catalog" })
    .boundingBox();
  const icon = await actions
    .getByRole("link", { name: "View on Hardcover" })
    .boundingBox();
  expect(
    Math.abs(add!.y + add!.height / 2 - icon!.y - icon!.height / 2),
  ).toBeLessThan(3);
  await page.screenshot({
    path: testInfo.outputPath("reviews-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("reviews-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/books/book?tab=editions");
  const section = page.getByRole("region", {
    name: "Editions and recordings",
    exact: true,
  });
  await expect(
    section.locator(".book-data-table").first().locator("tbody tr"),
  ).toHaveCount(20);
  await section
    .getByRole("button", { name: "Next editions page", exact: true })
    .click();
  await expect(section.locator(".book-data-table").first()).toContainText(
    "Edition 21",
  );
  expect(offsets).toContain(20);
  await section
    .getByRole("button", { name: "Previous editions page", exact: true })
    .click();
  await expect(section.locator(".book-data-table").first()).toContainText(
    "Edition 1",
  );
  expect(errors).toEqual([]);
});
