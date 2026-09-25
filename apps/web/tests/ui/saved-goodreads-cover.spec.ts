import { expect, test } from "../fixtures";

test("saved Goodreads covers survive refresh without metadata", async ({
  page,
}) => {
  const id = "00000000-0000-4000-8000-000000000042";
  const cover = "https://i.gr-assets.com/books/123.jpg";
  let synced = false;
  const work = {
    id,
    title: "Saved book",
    authors: ["Writer"],
    provisional: true,
    cover_url: cover,
    availability: { owned: false, ebook: false, audio: false },
  };
  const list = { id, name: "Want to read", count: 1, editable: true };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let data: unknown = { items: [], total: 0 };
    if (path === "/api/auth/me")
      data = {
        user: { id, role: "admin", username: "reader", display_name: "Reader" },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/metadata/account") data = { enabled: false };
    else if (path === "/api/discovery/layout") data = { order: [], hidden: [] };
    else if (path === "/api/lists/page") data = { items: [list], total: 1 };
    else if (path === `/api/lists/${id}`) data = { ...list, items: [work] };
    else if (path.endsWith("/subscription"))
      data = {
        provider: "goodreads",
        enabled: true,
        state: "idle",
        last_success_at: synced ? "2026-09-21T12:00:00Z" : null,
      };
    else if (path.endsWith("/subscription/sync")) {
      synced = true;
      data = { id, status: "queued" };
    } else if (path === "/api/catalog/cover-image") {
      expect(url.searchParams.get("url")).toBe(cover);
      return route.fulfill({
        contentType: "image/svg+xml",
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300"><rect width="200" height="300" fill="navy"/></svg>',
      });
    }
    if (
      new URL(route.request().url()).pathname.includes(
        "/acquisition/preferences/",
      )
    )
      data = { effective: { desired_media: "both" } };
    await route.fulfill({ json: data });
  });
  await page.goto("/discover?view=yours");
  const image = page.getByRole("img", { name: "Cover of Saved book" });
  await expect(image).toHaveAttribute(
    "src",
    `/api/catalog/cover-image?url=${encodeURIComponent(cover)}`,
  );
  await page
    .getByRole("button", { name: "Refresh Want to read", exact: true })
    .click();
  await expect.poll(() => synced).toBe(true);
  await expect(image).toBeVisible();
  await page.reload();
  await expect(image).toBeVisible();
  await expect
    .poll(() => image.evaluate((img: HTMLImageElement) => img.naturalWidth))
    .toBe(200);
});
