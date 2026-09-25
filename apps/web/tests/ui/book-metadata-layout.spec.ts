import { expect, test } from "../fixtures";

test("metadata edits, cover choices and read-only layout", async ({
  page,
}, testInfo) => {
  const cover = (color: string) =>
    `data:image/svg+xml,${encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="160" height="240"><rect width="160" height="240" fill="${color}"/><text x="18" y="100" fill="white" font-size="20">Quiet Harbor</text></svg>`)}`;
  const covers = [cover("#31505d"), cover("#705466")];
  let role = "admin";
  let work = {
    id: "book",
    title: "The Quiet Harbor",
    authors: ["Morgan Vale"],
    description: "A lighthouse keeper returns home.",
    publication_year: 2024,
    language: "en",
    cover_url: covers[0],
    availability: { owned: true, ebook: true, audio: false, stale: false },
  };
  const edits: unknown[] = [];
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = { items: [], total: 0 };
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role,
          username: "reader",
          display_name: "Reader",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/catalog/works/book") data = work;
    else if (path === "/api/metadata/works/book") {
      if (route.request().method() === "PATCH") {
        const body = route.request().postDataJSON();
        edits.push(body);
        work = { ...work, ...body.values };
      }
      data = {
        sources: [
          {
            id: "source",
            provider: "hardcover",
            external_id: "42",
            title: work.title,
            fetched_at: "2026-09-20T00:00:00Z",
            series: [],
            book: {},
            revision: "1",
          },
        ],
        fields: {
          title: { locked: true, provider: "manual" },
          description: { provider: "hardcover" },
        },
        cover_choices: covers,
        versions: [],
        versions_total: 0,
        enrichment: null,
      };
    } else if (path === "/api/metadata/books/hardcover/42")
      data = {
        book: {
          ...work,
          provider: "hardcover",
          external_id: "42",
          editions: [],
          subjects: [],
        },
      };
    else if (path.endsWith("/reader-details"))
      data = {
        external_id: "42",
        rating: 4.2,
        ratings_count: 100,
        authors: [],
        reviews: [],
      };
    else if (path.includes("/acquisition/preferences/"))
      data = { effective: { desired_media: "both" } };
    else if (path.includes("quick-add/latest")) data = null;
    await route.fulfill({ json: data });
  });
  await page.goto("/books/book?tab=manage");
  const metadata = page.getByRole("region", { name: "Manage book metadata" });
  await expect(
    metadata.getByRole("heading", { name: "Book metadata", exact: true }),
  ).toBeVisible();
  await expect(
    metadata.getByRole("button", { name: "Use cover 1" }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.screenshot({
    path: testInfo.outputPath("metadata-desktop.png"),
    fullPage: true,
  });
  await metadata
    .getByRole("button", { name: "Edit details", exact: true })
    .click();
  await expect(page.getByLabel("Book title", { exact: true })).toBeFocused();
  await page
    .getByLabel("Book title", { exact: true })
    .fill("A Different Harbor");
  await metadata.getByRole("button", { name: "Cancel", exact: true }).click();
  expect(edits).toEqual([]);
  await metadata
    .getByRole("button", { name: "Edit details", exact: true })
    .click();
  await expect(page.getByLabel("Book title", { exact: true })).toHaveValue(
    "The Quiet Harbor",
  );
  await page
    .getByLabel("Book title", { exact: true })
    .fill("A Different Harbor");
  await page
    .getByRole("button", { name: "Save protected edits", exact: true })
    .click();
  await expect(metadata.getByRole("status")).toContainText(
    "Book details updated.",
  );
  expect(edits).toEqual([{ values: { title: "A Different Harbor" } }]);
  await metadata.getByRole("button", { name: "Use cover 2" }).click();
  await expect(
    metadata.getByRole("button", { name: "Use cover 2" }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".reader-hero .book-cover img")).toHaveAttribute(
    "src",
    covers[1],
  );
  await page.setViewportSize({ width: 390, height: 844 });
  await metadata
    .getByRole("button", { name: "Edit details", exact: true })
    .click();
  await page.screenshot({
    path: testInfo.outputPath("metadata-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  role = "member";
  await page.reload();
  await expect(metadata.getByText("Read-only", { exact: true })).toBeVisible();
  await expect(
    metadata.getByRole("button", { name: "Edit details", exact: true }),
  ).toHaveCount(0);
  await expect(
    metadata.getByRole("button", { name: "Use cover 1" }),
  ).toBeDisabled();
  expect(errors).toEqual([]);
});
