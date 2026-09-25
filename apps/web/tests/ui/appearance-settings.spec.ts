import { expect, test } from "../fixtures";

test("cover shapes persist while legacy overlay preferences cannot hide overlays", async ({
  page,
}, testInfo) => {
  await page.addInitScript(() => {
    if (!localStorage.getItem("appearance-test-seeded")) {
      localStorage.setItem(
        "book-search:display:v1",
        JSON.stringify({
          defaultShape: "square",
          ownership: false,
          formats: false,
          missingFormats: false,
          ratings: false,
        }),
      );
      localStorage.setItem("appearance-test-seeded", "true");
    }
  });
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = { items: [], total: 0 };
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "viewer",
          username: "reader",
          display_name: "Reader",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/discovery/library")
      data = {
        title: "Recent library additions",
        status: "ready",
        items: [
          {
            book: {
              title: "The Long Way Home",
              authors: ["Alex Morgan"],
              rating: 4.5,
            },
            work: {
              id: "00000000-0000-0000-0000-000000000001",
              title: "The Long Way Home",
              authors: ["Alex Morgan"],
              availability: { owned: true, audio: true, ebook: false },
            },
          },
        ],
        has_more: false,
      };
    else if (path.includes("/cover")) return route.fulfill({ status: 404 });
    if (
      new URL(route.request().url()).pathname.includes(
        "/acquisition/preferences/",
      )
    )
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#display");
  const appearance = page.locator(".display-settings");
  await expect(appearance.getByRole("checkbox")).toHaveCount(0);
  const defaults = appearance.getByRole("group", {
    name: "Default covers",
    exact: true,
  });
  await expect(defaults.getByLabel("Square", { exact: true })).toBeChecked();
  await defaults.getByLabel("Book portrait").check();
  await appearance
    .getByRole("group", { name: "Audiobook covers" })
    .getByLabel("Square", { exact: true })
    .check();
  await page.reload();
  await expect(defaults.getByLabel("Book portrait")).toBeChecked();
  await expect(
    appearance
      .getByRole("group", { name: "Audiobook covers" })
      .getByLabel("Square", { exact: true }),
  ).toBeChecked();
  await page.screenshot({
    path: testInfo.outputPath("appearance-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("appearance-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.goto("/discover");
  const shelf = page.getByRole("region", {
    name: "Recent library additions",
    exact: true,
  });
  await expect(shelf.locator(".cover-owned")).toBeVisible();
  await expect(shelf.locator(".cover-format.is-missing")).toBeVisible();
  await expect(shelf.locator(".cover-format.is-owned")).toBeVisible();
});
