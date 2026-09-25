import { expect, test } from "../fixtures";

test("cover shortcuts acquire missing formats without navigating, including provider imports", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const writes: { work_id: string; specification: { mode?: string } }[] = [];
  const imports: string[] = [];
  let role = "admin";
  let fail = false;
  let queued = false;
  const art = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300"><rect width="200" height="300" fill="#7d4938"/><text x="20" y="90" fill="white" font-size="22">A NEW WORLD</text></svg>')}`;
  const items = ["missing", "ebook-owned", "complete", "provider"].map(
    (id, i) => ({
      book: {
        provider: "hardcover",
        external_id: id,
        title: id,
        authors: ["Test Author"],
        cover_url: art,
        rating: id === "complete" ? null : 4.25,
      },
      work:
        i === 3
          ? null
          : {
              id,
              title: id,
              authors: ["Test Author"],
              cover_url: art,
              availability: { owned: i > 0, ebook: i > 0, audio: i === 2 },
            },
    }),
  );
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = { items: [], total: 0 };
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          username: "reader",
          display_name: "Reader",
          role,
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/metadata/account") data = { enabled: true };
    else if (path.endsWith("/cover")) return route.fulfill({ status: 404 });
    else if (path === "/api/discovery/hardcover/trending")
      data = { title: "Trending on Hardcover", status: "ready", items };
    else if (path.startsWith("/api/discovery/"))
      data = { title: "Other books", status: "ready", items: [], total: 0 };
    else if (path === "/api/acquisition/preferences/personal")
      data = { effective: { desired_media: "both" } };
    else if (path.includes("/quick-add/latest/")) data = null;
    else if (path.endsWith("/import")) {
      imports.push(path);
      data = { id: "imported" };
    } else if (path === "/api/requests/quick-add") {
      writes.push(route.request().postDataJSON());
      if (fail)
        return route.fulfill({
          status: 503,
          json: { detail: "Please retry download" },
        });
      data = {
        id: "receipt",
        status: queued ? "queued" : "completed",
        message: queued ? "Searching for releases" : "Downloads queued",
      };
    }
    await route.fulfill({ json: data });
  });
  await page.goto("/discover");
  await expect(page.locator(".cover-rating")).toHaveCount(3);
  await expect(page.locator(".cover-rating").first()).toHaveText("4.3");
  const missing = page.locator(".book-card").filter({
    has: page.getByRole("heading", { name: "missing", exact: true }),
  });
  const owned = page.locator(".book-card").filter({
    has: page.getByRole("heading", { name: "ebook-owned", exact: true }),
  });
  const complete = page.locator(".book-card").filter({
    has: page.getByRole("heading", { name: "complete", exact: true }),
  });
  const provider = page.locator(".book-card").filter({
    has: page.getByRole("heading", { name: "provider", exact: true }),
  });
  await expect(missing).toBeVisible();
  await expect(
    missing.getByRole("button", { name: "Quick add from cover" }),
  ).toBeHidden();
  await missing.hover();
  await expect(
    missing.getByRole("button", { name: "Download ebook", exact: true }),
  ).toBeVisible();
  await expect(missing.locator(".cover-quick-add")).toHaveCSS("opacity", "1");
  await page.screenshot({ path: testInfo.outputPath("cover-hover.png") });
  await missing.getByRole("button", { name: "Quick add from cover" }).click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual({ work_id: "missing", specification: {} });
  await expect(page).toHaveURL(/\/discover$/);
  await missing
    .getByRole("button", { name: "Download ebook", exact: true })
    .click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1].specification).toEqual({ mode: "ebook" });
  await owned.getByRole("link").focus();
  await expect(
    owned.getByRole("button", { name: "Ebook already in library" }),
  ).toBeDisabled();
  await owned
    .getByRole("button", { name: "Download audiobook", exact: true })
    .click();
  await expect.poll(() => writes.length).toBe(3);
  expect(writes[2]).toEqual({
    work_id: "ebook-owned",
    specification: { mode: "audio" },
  });
  await complete.hover();
  await expect(complete.locator(".cover-quick-add")).toHaveCount(0);
  fail = true;
  await provider.hover();
  await provider
    .getByRole("button", { name: "Download audiobook", exact: true })
    .click();
  await expect(provider.getByRole("alert")).toContainText(
    "Please retry download",
  );
  fail = false;
  queued = true;
  await provider
    .getByRole("button", { name: "Download audiobook", exact: true })
    .click();
  await expect(
    provider.getByRole("button", { name: "Download audiobook", exact: true }),
  ).toBeDisabled();
  expect(imports).toHaveLength(1);
  expect(writes.at(-1)).toEqual({
    work_id: "imported",
    specification: { mode: "audio" },
  });
  expect(await page.locator("a button").count()).toBe(0);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("cover-mobile.png"),
    fullPage: true,
  });
  role = "viewer";
  await page.reload();
  await expect(missing).toBeVisible();
  await expect(page.locator(".cover-quick-add")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("cover quick add links to preferences when no default media is saved", async ({
  page,
}) => {
  const writes: unknown[] = [];
  const art = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="200" height="300"><rect width="200" height="300" fill="#243044"/><text x="16" y="150" fill="white" font-size="18">COVER</text></svg>')}`;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = { items: [], total: 0 };
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          username: "reader",
          display_name: "Reader",
          role: "admin",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/metadata/account") data = { enabled: true };
    else if (path.endsWith("/cover")) return route.fulfill({ status: 404 });
    else if (path === "/api/discovery/hardcover/trending")
      data = {
        title: "Trending on Hardcover",
        status: "ready",
        items: [
          {
            book: {
              provider: "hardcover",
              external_id: "unset",
              title: "Unset preference",
              authors: ["Test Author"],
              cover_url: art,
            },
            work: {
              id: "unset",
              title: "Unset preference",
              authors: ["Test Author"],
              cover_url: art,
              availability: { owned: false, ebook: false, audio: false },
            },
          },
        ],
      };
    else if (path.startsWith("/api/discovery/"))
      data = { title: "Other books", status: "ready", items: [], total: 0 };
    else if (path === "/api/acquisition/preferences/personal")
      data = { effective: { desired_media: null } };
    else if (path.includes("/quick-add/latest/")) data = null;
    else if (path === "/api/requests/quick-add") {
      writes.push(route.request().postDataJSON());
      data = {
        id: "receipt",
        status: "completed",
        message: "Downloads queued",
      };
    }
    await route.fulfill({ json: data });
  });
  await page.goto("/discover");
  const card = page.locator(".book-card").filter({
    has: page.getByRole("heading", { name: "Unset preference", exact: true }),
  });
  await card.hover();
  const quickAdd = card.getByRole("button", { name: "Quick add from cover" });
  await expect(quickAdd).toHaveAttribute(
    "title",
    "Quick add preference not set",
  );
  await quickAdd.click();
  await expect(card.getByText("Preference not set")).toBeVisible();
  const settings = card.getByRole("link", { name: "Set here" });
  await expect(settings).toBeVisible();
  await expect(settings).toHaveAttribute("href", "/settings#preferences");
  await expect(card.getByRole("alert")).toHaveCount(0);
  expect(writes).toEqual([]);
  await card
    .getByRole("button", { name: "Download ebook", exact: true })
    .click();
  await expect.poll(() => writes.length).toBe(1);
  await quickAdd.click();
  await card.getByRole("link", { name: "Set here" }).click();
  await expect(page).toHaveURL(/\/settings#preferences$/);
});
