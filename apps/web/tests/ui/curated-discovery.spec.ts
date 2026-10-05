import { expect, test } from "../fixtures";

test("award filters, follow controls and recording selection preserve the distinction from downloads", async ({
  page,
}, testInfo) => {
  const collection = {
    id: "official-audie-audiobook-2025",
    kind: "award",
    provider: "audie",
    title: "Audie · Audiobook of the Year",
    year: 2025,
    category: "Audiobook of the Year",
    genres: ["audiobooks"],
    count: 1,
    coverage: "complete",
    updated_at: "2026-10-05T00:00:00Z",
    covers: [],
    pinned: false,
    tracking: false,
    saved: false,
    medium: "audio",
    source_url: "https://www.audiopub.org/2025audies-1",
    refresh_mode: "app-update",
    description: "The winning performance. Choose the recognized recording.",
  };
  const entry = {
    external_id: "audie-barbra",
    provider: "audie",
    title: "My Name Is Barbra",
    authors: ["Barbra Streisand"],
    narrators: ["Barbra Streisand"],
    subject: "recording",
    status: "winner",
    winner: true,
    identifiers: {},
    source_url: collection.source_url,
  };
  const mutations: string[] = [];
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    let data: unknown = { items: [], total: 0 };
    if (route.request().method() !== "GET") mutations.push(url.pathname);
    if (url.pathname === "/api/auth/me")
      data = {
        csrf_token: "qa",
        user: {
          id: "reader",
          username: "reader",
          role: "admin",
          display_name: "Reader",
        },
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "completed" };
    else if (url.pathname === "/api/metadata/account")
      data = { enabled: false };
    else if (url.pathname === "/api/discovery/layout")
      data = { order: [], hidden: [] };
    else if (url.pathname.endsWith("/follow")) {
      Object.assign(collection, route.request().postDataJSON(), {
        saved: true,
      });
      data = collection;
    } else if (url.pathname === "/api/discovery/collections")
      data = {
        items: [collection],
        total: 1,
        years: [2025],
        genres: ["audiobooks"],
        categories: [collection.category],
        archive_gaps: [],
        providers: ["audie", "hugo"],
        audiences: ["adult"],
        languages: ["en"],
      };
    else if (url.pathname === `/api/discovery/collections/${collection.id}`)
      data = {
        collection,
        items: [entry],
        total: 1,
        page: 1,
        has_more: false,
      };
    else if (url.pathname === "/api/discovery/curation/audie-barbra")
      data = {
        entry,
        match: {
          status: "needs-review",
          candidates: [],
          book: null,
          reason:
            "This selection recognizes a specific edition or performance. Choose its edition before requesting.",
        },
      };
    await route.fulfill({ json: data });
  });
  await page.goto("/discover?view=awards");
  await expect(
    page.getByRole("heading", { name: "Stories worth celebrating" }),
  ).toBeVisible();
  await page
    .getByRole("combobox", { name: "Source", exact: true })
    .selectOption("audie");
  await expect(page).toHaveURL(/source=audie/);
  await page
    .getByRole("link")
    .filter({ has: page.getByRole("heading", { name: collection.title }) })
    .click();
  await expect(
    page.getByText("Recording selection", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /Download/ })).toHaveCount(0);
  await page
    .getByRole("button", { name: "Follow updates", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Following updates" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Show on For you" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Show on For you" }).click();
  await expect(
    page.getByRole("button", { name: "Following updates" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Following updates" }).click();
  await expect(
    page.getByRole("button", { name: "Follow updates", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Winner", exact: true }).click();
  await expect(page).toHaveURL(/winners=1/);
  await page.screenshot({
    path: testInfo.outputPath("award-collection.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("link", { name: "View My Name Is Barbra" }).click();
  await expect(
    page.getByText(/Choose its edition before requesting/),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Find editions" })).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Find editions" }),
  ).toHaveAttribute("href", /provider=audible/);
  await page.getByRole("link", { name: "Back to Discover" }).click();
  await expect(page).toHaveURL(/winners=1/);
  await page.getByRole("link", { name: "Back to Discover" }).click();
  await expect(page).toHaveURL(/view=awards&source=audie/);
  await page.getByRole("button", { name: "Clear filters" }).click();
  await expect(page).toHaveURL(/discover\?view=awards$/);
  expect(mutations).toEqual([
    `/api/discovery/collections/${collection.id}/follow`,
    `/api/discovery/collections/${collection.id}/follow`,
    `/api/discovery/collections/${collection.id}/follow`,
  ]);
});

for (const catalogued of [false, true]) {
  test(`automatic fallback keeps recording choices for ${catalogued ? "catalogued" : "new"} books`, async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const writes: string[] = [];
    const searches: string[] = [];
    const id = "00000000-0000-0000-0000-000000000042";
    const work = {
      id,
      title: "Harbor",
      authors: ["Writer"],
      provisional: false,
      availability: { owned: false, ebook: false, audio: false, stale: false },
    };
    const edition = {
      external_id: "B012345678",
      title: "Harbor recording",
      medium: "audio",
      narrators: ["Audio Narrator"],
      language: "en",
      identifiers: { asin: "B012345678" },
      runtime_minutes: 620,
    };
    const book = {
      provider: "audible",
      external_id: "B012345678",
      title: "Harbor",
      authors: ["Writer"],
      editions: [edition],
      subjects: [],
      series: [],
    };
    await page.route("**/api/**", async (route) => {
      const url = new URL(route.request().url());
      if (route.request().method() !== "GET") writes.push(url.pathname);
      let data: unknown = { items: [], total: 0 };
      if (url.pathname === "/api/auth/me")
        data = {
          csrf_token: "qa",
          user: {
            id: "reader",
            username: "reader",
            role: "member",
            display_name: "Reader",
            permissions: ["request", "request_advanced", "auto_approve"],
          },
        };
      else if (url.pathname === "/api/setup/onboarding")
        data = { status: "completed" };
      else if (url.pathname === "/api/metadata/account")
        data = { enabled: false };
      else if (url.pathname === "/api/metadata/search") {
        searches.push(
          `${url.searchParams.get("provider")}:${url.searchParams.get("page")}`,
        );
        data = {
          provider: "audible",
          page: Number(url.searchParams.get("page")),
          items:
            url.searchParams.get("page") === "1"
              ? [book]
              : [
                  {
                    ...book,
                    external_id: "B012345679",
                    title: "Another recording",
                  },
                ],
          has_more: url.searchParams.get("page") === "1",
          known_works: catalogued ? { B012345678: work, B012345679: work } : {},
        };
      } else if (url.pathname === "/api/catalog/works")
        data = { items: catalogued ? [work] : [], total: catalogued ? 1 : 0 };
      else if (url.pathname === "/api/metadata/books/audible/B012345678")
        data = { book, work: catalogued ? work : null };
      else if (url.pathname.endsWith("/import")) data = work;
      else if (url.pathname === `/api/catalog/works/${id}`) data = work;
      else if (url.pathname === `/api/metadata/works/${id}`)
        data = {
          versions: [
            { ...edition, id: "version", owned: false, needs_review: false },
          ],
          versions_total: 1,
          sources: [],
          fields: {},
          cover_choices: [],
        };
      else if (url.pathname.endsWith("/reader-match"))
        data = { status: "disabled", candidates: [] };
      else if (url.pathname.startsWith("/api/acquisition/preferences/"))
        data = { effective: { desired_media: "audio" } };
      else if (url.pathname === "/api/following") data = [];
      await route.fulfill({ json: data });
    });
    await page.goto("/search?q=Harbor&provider=automatic");
    await expect(
      page.getByRole("link", { name: "View Another recording" }),
    ).toBeVisible();
    expect(searches).toEqual(["automatic:1", "audible:2"]);
    await page.locator('a[href="/discover/books/audible/B012345678"]').click();
    await expect(page.getByRole("button", { name: /Quick add/ })).toHaveCount(
      0,
    );
    await expect(
      page.getByRole("button", { name: "Review recording" }),
    ).toBeVisible();
    await page.getByRole("button", { name: "Review recording" }).click();
    await expect(page).toHaveURL(new RegExp(`/books/${id}\\?tab=editions`));
    await expect(
      page.getByRole("button", { name: "Request recording", exact: true }),
    ).toBeVisible();
    if (catalogued)
      expect(writes).not.toContain(
        "/api/metadata/books/audible/B012345678/import",
      );
    else
      expect(writes).toContain("/api/metadata/books/audible/B012345678/import");
    expect(writes.filter((path) => path.startsWith("/api/requests"))).toEqual(
      [],
    );
    expect(errors).toEqual([]);
  });
}
