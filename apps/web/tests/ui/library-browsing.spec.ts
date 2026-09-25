import { expect, test } from "../fixtures";

test("library browsing searches books and preserves filters through navigation", async ({
  page,
}, testInfo) => {
  test.setTimeout(60_000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const first = {
    id: "book-1",
    title: "A Wizard of Earthsea",
    authors: ["Ursula Le Guin"],
    library_id: "11111111-1111-4111-8111-111111111111",
    availability: { owned: true, ebook: true, audio: false, stale: false },
  };
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    let data: unknown = {};
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
    else if (url.pathname === "/api/library/libraries")
      data = [{ id: first.library_id, name: "My books" }];
    else if (url.pathname === "/api/library/books")
      data = { items: [first], total: 1, offset: 0, limit: 40 };
    if (
      new URL(route.request().url()).pathname.includes(
        "/acquisition/preferences/",
      )
    )
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
  const writes: string[] = [];
  page.on("request", (request) => {
    if (!["GET", "HEAD", "OPTIONS"].includes(request.method()))
      writes.push(request.url());
  });
  await page.goto("/review?review=false&offset=999");
  const copies = page.getByRole("region", {
    name: "Library books",
    exact: true,
  });
  await expect(page).toHaveURL(/\/library$/);
  await expect(
    page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: "Review", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByLabel("Review queue")).toHaveCount(0);
  await page
    .getByRole("combobox", { name: "Media", exact: true })
    .selectOption("ebook");
  await expect(page).not.toHaveURL(/offset=/);
  await expect(copies.locator(".book-card").first()).toBeVisible();
  await page
    .getByRole("searchbox", { name: "Search your library" })
    .fill(first.title);
  await page
    .getByRole("button", { name: "Search library", exact: true })
    .click();
  await expect(page).toHaveURL(/q=/);
  await expect(copies.locator(".book-card")).toHaveCount(1);
  await page
    .getByRole("combobox", { name: "Sort books" })
    .selectOption("recent");
  await expect(page).toHaveURL(/sort=recent/);
  await page
    .getByRole("combobox", { name: "Library", exact: true })
    .selectOption(first.library_id);
  await expect(page).toHaveURL(new RegExp(`library=${first.library_id}`));
  await expect(
    page.getByRole("searchbox", { name: "Search your library" }),
  ).toHaveValue(first.title);
  await expect(
    page.getByRole("combobox", { name: "Media", exact: true }),
  ).toHaveValue("ebook");
  await expect(
    page.getByRole("combobox", { name: "Library", exact: true }),
  ).toHaveValue(first.library_id);
  await expect(page.getByRole("combobox", { name: "Sort books" })).toHaveValue(
    "recent",
  );
  await expect(copies.locator(".book-card").first()).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("library-browsing-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("library-browsing-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "Reset library view" }).click();
  await expect(page).toHaveURL(/\/library$/);
  await expect(copies.locator(".book-card").first()).toBeVisible();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});
