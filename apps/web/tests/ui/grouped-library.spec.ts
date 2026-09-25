import { expect, test } from "../fixtures";

test("one library card shows both formats and uses the mixed-view cover preference", async ({
  page,
}, testInfo) => {
  await page.addInitScript(() => {
    const key = "book-search:display:v1";
    if (!localStorage.getItem(key))
      localStorage.setItem(key, JSON.stringify({ audioShape: "square" }));
  });
  const work = {
    id: "00000000-0000-0000-0000-000000000123",
    title: "The Giver of Stars",
    authors: ["Jojo Moyes"],
    availability: { owned: true, ebook: true, audio: true, stale: false },
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
    else if (url.pathname === "/api/library/libraries") data = [];
    else if (url.pathname.endsWith("/cover"))
      return route.fulfill({
        contentType: "image/svg+xml",
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600"><rect width="400" height="600" fill="#192d51"/><text x="45" y="200" fill="#e9c789" font-size="28">The Giver of Stars</text></svg>',
      });
    else if (
      ["/api/catalog/works", "/api/library/books"].includes(url.pathname)
    )
      data = {
        items: [work],
        total: 1,
        offset: 0,
        limit: 40,
      };
    if (
      url.pathname === "/api/catalog/works" &&
      url.searchParams.get("q") === "recording"
    ) {
      data = { items: [], total: 0, offset: 0, limit: 40 };
    }
    if (url.pathname === "/api/metadata/search") {
      data = {
        provider: "hardcover",
        page: 1,
        has_more: false,
        items: ["ebook", "audio"].map((external_id) => ({
          provider: "hardcover",
          external_id,
          title: work.title,
          authors: work.authors,
        })),
        known_works: { ebook: work, audio: work },
      };
    }
    if (
      new URL(route.request().url()).pathname.includes(
        "/acquisition/preferences/",
      )
    )
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
  await page.goto("/library");
  const library = page.getByRole("region", {
    name: "Library books",
    exact: true,
  });
  const card = library.locator(".book-card");
  await expect(card).toHaveCount(1);
  await expect(card.locator(".cover-portrait")).toBeVisible();
  await expect(
    card.getByRole("img", { name: "Ebook: in library", exact: true }),
  ).toBeVisible();
  await expect(
    card.getByRole("img", { name: "Audiobook: in library", exact: true }),
  ).toBeVisible();
  await expect(card.locator("img")).toHaveAttribute("src", /medium=ebook$/);
  await page
    .getByRole("combobox", { name: "Media", exact: true })
    .selectOption("audio");
  await expect(card).toHaveCount(1);
  await expect(card.locator(".cover-square")).toBeVisible();
  await expect(card.locator("img")).toHaveAttribute("src", /medium=audio$/);
  await page.goto("/settings#display");
  await page
    .getByRole("group", { name: "Default covers", exact: true })
    .getByLabel("Square", { exact: true })
    .check();
  await page.goto("/library");
  await expect(card.locator(".cover-square")).toBeVisible();
  await expect(card.locator("img")).toHaveAttribute("src", /medium=ebook$/);
  await page.reload();
  await expect(card.locator(".cover-square")).toBeVisible();
  await page.goto("/library?view=saved");
  await expect(page.locator(".book-card")).toHaveCount(1);
  await expect(page.locator(".book-card img")).toHaveAttribute(
    "src",
    /medium=ebook$/,
  );
  await page
    .getByRole("combobox", { name: "Format", exact: true })
    .selectOption("audio");
  await expect(page.locator(".book-card img")).toHaveAttribute(
    "src",
    /medium=audio$/,
  );
  await page.goto("/settings#display");
  await page.getByRole("button", { name: "Reset cover shapes" }).click();
  await page.goto("/library");
  await expect(card.locator(".cover-portrait")).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("grouped-library.png"),
    fullPage: true,
  });
  await page.goto("/search?q=Giver");
  await expect(page.locator(".book-card")).toHaveCount(1);
  await expect(page.locator(".provider-result")).toHaveCount(0);
  await page.goto("/search?q=recording");
  await expect(page.locator(".provider-result")).toHaveCount(1);
  await expect(page.locator(".provider-result img")).toHaveAttribute(
    "src",
    /medium=ebook$/,
  );
});
