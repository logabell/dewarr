import { expect, test, type Page } from "../fixtures";

async function fixture(page: Page) {
  let exists = false;
  let excluded = false;
  let active = false;
  let enabled = true;
  let generation = 1;
  let lastPreview: Record<string, unknown> = {};
  const filters = {
    compilations: false,
    box_sets: false,
    anthologies: false,
    non_main_series: false,
    coauthored: true,
    language: null,
  };
  const profile = {
    id: null,
    name: "My download settings",
    generation: 0,
    effective_revision: "a".repeat(64),
    preferences: { desired_media: "audio" },
    origins: {},
    scope_origins: {},
  };
  const configuration = {
    mode: "browse",
    specification: { mode: "audio" },
    profile,
    downloader_id: null,
    downloader_generation: null,
    routes: {},
  };
  const follow = () => ({
    list_id: "follow-list",
    name: "Ursula K. Le Guin",
    source_kind: "author",
    external_id: "9",
    filters,
    mode: "browse",
    active,
    subscription: {
      id: "subscription",
      generation,
      enabled,
      state: "idle",
      provider: "hardcover",
      source_kind: "author",
      message: enabled ? "Catalog verified: 2 books" : "Follow paused",
      observed_count: 2,
      excluded_count: excluded ? 1 : 0,
      completeness: "verified-observation",
      last_success_at: "2026-09-23T12:00:00Z",
    },
  });
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "member",
          username: "reader",
          display_name: "Reader",
          permissions: ["request", "automate"],
          onboarding_status: "complete",
        },
        csrf_token: "fixture",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/metadata/authors/hardcover/9")
      data = {
        author: { external_id: "9", name: "Ursula K. Le Guin" },
        books: [],
        has_more: false,
        page: 1,
        known_works: {},
      };
    else if (path === "/api/following") {
      if (method === "POST") {
        exists = true;
        data = follow();
      } else data = exists ? [follow()] : [];
    } else if (path === "/api/following/follow-list") {
      if (method === "DELETE") {
        exists = false;
        return route.fulfill({ status: 204 });
      }
      enabled = route.request().postDataJSON().enabled;
      generation++;
      data = follow();
    } else if (path.endsWith("/subscription/observations/book-one")) {
      excluded = route.request().postDataJSON().excluded;
      return route.fulfill({ status: 204 });
    } else if (path.endsWith("/subscription/observations"))
      data = {
        total: 2,
        offset: 0,
        limit: 50,
        items: [
          {
            id: "book-one",
            work_id: "work-one",
            title: "A Wizard of Earthsea",
            excluded,
            present: true,
            filter_reason: null,
          },
          {
            id: "book-two",
            work_id: "work-two",
            title: "Earthsea Box Set",
            excluded: false,
            present: true,
            filter_reason: "Box set",
          },
        ],
      };
    else if (path.endsWith("/acquisition/books"))
      data = { items: [], total: 0 };
    else if (path === "/api/acquisition/selections/options")
      data = { downloaders: [], destinations: [] };
    else if (path === "/api/acquisition/profiles") data = [profile];
    else if (path.endsWith("/acquisition/preview")) {
      lastPreview = route.request().postDataJSON();
      data = { id: "preview" };
    } else if (path.endsWith("/previews/preview"))
      data = {
        id: "preview",
        configuration,
        records: [],
        total: 1,
        selected: 0,
        counts: { owned: 0, missing: 1, excluded: 1 },
      };
    else if (path.endsWith("/activate")) {
      active = true;
      data = {
        id: "policy",
        revision: 1,
        generation: 1,
        active,
        configuration,
        counts: {},
        message: "Browse mode",
      };
    } else if (path.endsWith("/acquisition"))
      data = active
        ? {
            id: "policy",
            revision: 1,
            generation: 1,
            active,
            configuration,
            counts: {},
            message: "Browse mode",
          }
        : null;
    return route.fulfill({ json: data });
  });
  return { preview: () => lastPreview };
}

test("author follow verifies catalog, defaults to future only, and keeps exclusions", async ({
  page,
}, testInfo) => {
  const state = await fixture(page);
  await page.goto("/authors/hardcover/9");
  await page.getByRole("link", { name: "Follow author" }).click();
  await expect(
    page.getByRole("checkbox", { name: "Include compilations" }),
  ).not.toBeChecked();
  await expect(
    page.getByRole("checkbox", { name: "Include box sets" }),
  ).not.toBeChecked();
  await expect(
    page.getByRole("checkbox", { name: "Include anthologies" }),
  ).not.toBeChecked();
  await page
    .getByRole("button", { name: "Follow and preview catalog" })
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "Catalog verified" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Preview follow policy" }).click();
  expect(state.preview().include_work_ids).toEqual([]);
  await expect(
    page.getByText("0 owned · 1 missing · 1 excluded"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Save list mode" }).click();
  await page
    .getByRole("button", { name: "Exclude book", exact: true })
    .first()
    .click();
  await expect(
    page.getByText("Excluded by you", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("following-desktop.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "Pause follow", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Resume follow" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Remove exclusion" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Unfollow", exact: true }).click();
  await expect(
    page.getByText(
      "Open an author or series page and choose Follow to get started.",
    ),
  ).toBeVisible();
});

test("Following is usable at a narrow viewport", async ({ page }, testInfo) => {
  await fixture(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/following?kind=series&externalId=7&name=Earthsea");
  await expect(
    page.getByRole("heading", { name: "Follow Earthsea" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Follow and preview catalog" }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: testInfo.outputPath("following-mobile.png"),
    fullPage: true,
  });
});
