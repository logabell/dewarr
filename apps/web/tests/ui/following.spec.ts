import { expect, test, type Page } from "../fixtures";

async function fixture(page: Page, initiallyFollowing = false) {
  let exists = initiallyFollowing;
  const extraFollows = new Set<number>();
  const created: number[] = [];
  let excluded = false;
  let active = false;
  let enabled = true;
  let generation = 1;
  let refreshing = false;
  let catalogRevision = "initial";
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
  const follow = (id = 9) => ({
    list_id: id === 9 ? "follow-list" : "follow-list-two",
    name: id === 9 ? "Ursula K. Le Guin" : "Another Author",
    source_kind: "author",
    external_id: String(id),
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
  const books = [
    {
      work_id: "work-one",
      external_id: "42",
      title: "A Wizard of Earthsea",
      authors: ["Ursula K. Le Guin"],
      cover_url: "https://assets.hardcover.app/wizard.jpg",
      release_date: "1968-01-01",
      upcoming: false,
      included: true,
      ebook: true,
      audio: false,
      stale: false,
      follow_names: [],
    },
    {
      work_id: "work-two",
      external_id: "43",
      title: "The Tombs of Atuan",
      authors: ["Ursula K. Le Guin"],
      cover_url: "https://assets.hardcover.app/atuan.jpg",
      release_date: "1971-01-01",
      upcoming: false,
      included: true,
      ebook: false,
      audio: false,
      stale: false,
      follow_names: [],
    },
    {
      work_id: "work-three",
      external_id: "44",
      title: "Sample future publication",
      authors: ["Ursula K. Le Guin"],
      cover_url: null,
      release_date: "2100-10-20",
      upcoming: true,
      included: true,
      ebook: false,
      audio: false,
      stale: false,
      follow_names: ["Ursula K. Le Guin"],
    },
  ];
  const catalogBooks = () => (refreshing ? books.slice(0, 2) : books);
  const summary = () => ({
    list_id: "follow-list",
    name: "Ursula K. Le Guin",
    source_kind: "author",
    external_id: "9",
    image_url: null,
    followed_at: "2026-09-23T12:00:00Z",
    enabled,
    state: refreshing ? "queued" : "idle",
    message: "Catalog verified",
    complete: true,
    last_success_at:
      catalogRevision === "initial"
        ? "2026-09-23T12:00:00Z"
        : "2026-09-28T12:00:00Z",
    mode: "browse",
    active,
    total_books: catalogBooks().length,
    library_books: 1,
    upcoming_books: refreshing ? 0 : 1,
    undated_books: 0,
    recent_books: 0,
    missing_books: 1,
    next_release: refreshing ? null : books[2],
    latest_books: [books[1], books[0]],
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (path === "/api/catalog/cover-image") return route.fallback();
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
    else if (path.startsWith("/api/acquisition/preferences/"))
      data = { effective: { desired_media: "both" } };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/metadata/authors/hardcover/9")
      data = {
        author: { external_id: "9", name: "Ursula K. Le Guin" },
        books: [],
        has_more: false,
        page: 1,
        known_works: {},
      };
    else if (path === "/api/following/overview") {
      const matches =
        exists &&
        !(refreshing && url.searchParams.get("filter") === "upcoming") &&
        url.searchParams.get("kind") !== "series" &&
        "ursula k. le guin".includes(
          (url.searchParams.get("q") || "").toLowerCase(),
        );
      data = {
        items: matches ? [summary()] : [],
        total: matches ? 1 : 0,
        authors: exists ? 1 : 0,
        series: 0,
        pending_sync: refreshing,
        catalog_revision: catalogRevision,
        offset: 0,
        limit: 20,
      };
    } else if (path === "/api/following/releases") {
      data = {
        items: exists && !refreshing ? [books[2]] : [],
        total: exists && !refreshing ? 1 : 0,
        offset: 0,
        limit: 24,
      };
    } else if (path === "/api/following/follow-list/books") {
      const filter = url.searchParams.get("filter");
      const selected = catalogBooks().filter((b) =>
        filter === "library"
          ? b.ebook
          : filter === "upcoming"
            ? b.upcoming
            : true,
      );
      data = { items: selected, total: selected.length, offset: 0, limit: 12 };
    } else if (path.endsWith("/reader-details")) {
      data = {
        external_id: "42",
        authors: [
          { external_id: "9", name: "Ursula K. Le Guin" },
          { external_id: "10", name: "Another Author" },
        ],
        reviews: [],
      };
    } else if (path === "/api/catalog/works/work-one") {
      data = {
        id: "work-one",
        title: "A Wizard of Earthsea",
        authors: ["Ursula K. Le Guin", "Another Author"],
        availability: { owned: true, ebook: true, audio: false, stale: false },
      };
    } else if (path === "/api/metadata/works/work-one") {
      data = {
        sources: [
          { provider: "hardcover", external_id: "42", book: {}, series: [] },
        ],
        fields: {},
        versions: [],
        versions_total: 0,
      };
    } else if (path === "/api/metadata/books/hardcover/42") {
      data = {
        book: {
          provider: "hardcover",
          external_id: "42",
          title: "A Wizard of Earthsea",
          authors: ["Ursula K. Le Guin", "Another Author"],
          editions: [],
        },
        work: null,
      };
    } else if (path.includes("quick-add/latest")) data = null;
    else if (path === "/api/following") {
      if (method === "POST") {
        const id = route.request().postDataJSON().external_id;
        created.push(id);
        if (id === 9) exists = true;
        else extraFollows.add(id);
        data = follow(id);
      } else
        data = [
          ...(exists ? [follow()] : []),
          ...[...extraFollows].map((id) => follow(id)),
        ];
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
  return {
    preview: () => lastPreview,
    created,
    startRefresh: () => {
      refreshing = true;
    },
    completeRefresh: () => {
      refreshing = false;
      catalogRevision = "completed";
    },
  };
}

test("author follow verifies catalog, defaults to future only, and keeps exclusions", async ({
  page,
}, testInfo) => {
  const state = await fixture(page);
  await page.goto("/authors/hardcover/9");
  await page
    .getByRole("button", { name: "Follow author", exact: true })
    .click();
  await expect(page).toHaveURL(/authors\/hardcover\/9$/);
  await page
    .getByRole("link", { name: "Manage following Ursula K. Le Guin" })
    .click();
  await page.getByText("Edit follow filters", { exact: true }).click();
  await expect(
    page.getByRole("checkbox", { name: "Include compilations" }),
  ).not.toBeChecked();
  await expect(
    page.getByRole("checkbox", { name: "Include box sets" }),
  ).not.toBeChecked();
  await expect(
    page.getByRole("checkbox", { name: "Include anthologies" }),
  ).not.toBeChecked();

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

test("Following shows library coverage, releases, and useful filtered books", async ({
  page,
}, testInfo) => {
  await fixture(page, true);
  await page.goto("/following");
  const author = page.getByRole("article", {
    name: "Ursula K. Le Guin",
    exact: true,
  });
  await expect(
    author.getByRole("button", { name: "3 books in catalog" }),
  ).toBeVisible();
  await expect(
    author.getByRole("button", { name: "1 in library" }),
  ).toBeVisible();
  await expect(
    author.getByRole("button", { name: "1 upcoming" }),
  ).toBeVisible();
  await author.getByRole("button", { name: "1 in library" }).click();
  await expect(
    author.getByRole("heading", { name: "In your library", exact: true }),
  ).toBeVisible();
  await expect(
    author
      .locator(".follow-expanded")
      .getByRole("link", { name: "A Wizard of Earthsea", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("following-overview-desktop.png"),
    fullPage: true,
  });
  await page.getByLabel("Search followed authors").fill("Nobody");
  await page
    .getByRole("button", { name: "Search follows", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "No matching follows" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Clear filters" }).click();
  await page.getByRole("tab", { name: "Releases", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Releases from your follows" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Sample future publication", exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: /Authors/ }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    author.getByRole("link", { name: "Manage Ursula K. Le Guin" }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: testInfo.outputPath("following-overview-mobile.png"),
    fullPage: true,
  });
});

for (const path of ["/books/work-one", "/discover/books/hardcover/42"]) {
  test("follow specific co-authors in place from " + path, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    const state = await fixture(page);
    await page.goto(path);
    await page.getByText("Follow authors", { exact: true }).click();
    const first = page.locator(".book-author-follows li").filter({
      has: page.getByRole("link", { name: "Ursula K. Le Guin", exact: true }),
    });
    const second = page.locator(".book-author-follows li").filter({
      has: page.getByRole("link", { name: "Another Author", exact: true }),
    });
    await first
      .getByRole("button", { name: "Follow author", exact: true })
      .click();
    await expect(
      first.getByRole("link", { name: "Manage following Ursula K. Le Guin" }),
    ).toBeVisible();
    await expect(
      second.getByRole("button", { name: "Follow author", exact: true }),
    ).toBeVisible();
    expect(state.created).toEqual([9]);
    expect(new URL(page.url()).pathname).toBe(path);
    await first
      .getByRole("link", { name: "Ursula K. Le Guin", exact: true })
      .click();
    await expect(
      page.getByRole("link", { name: "Manage following Ursula K. Le Guin" }),
    ).toBeVisible();
    expect(errors).toEqual([]);
  });
}

test("failed follow stays on the author page and can be retried", async ({
  page,
}) => {
  await fixture(page);
  let disconnected = true;
  await page.route("**/api/following", async (route) => {
    if (route.request().method() === "POST" && disconnected)
      return route.fulfill({
        status: 409,
        json: {
          detail: "Connect and enable your Hardcover account in Metadata first",
        },
      });
    return route.fallback();
  });
  await page.goto("/authors/hardcover/9");
  await page
    .getByRole("button", { name: "Follow author", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("Connect and enable");
  await expect(
    page.getByRole("link", { name: "Connection settings" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Follow author", exact: true }),
  ).toBeEnabled();
  expect(new URL(page.url()).pathname).toBe("/authors/hardcover/9");
  disconnected = false;
  await page
    .getByRole("button", { name: "Follow author", exact: true })
    .click();
  await expect(
    page.getByRole("link", { name: "Manage following Ursula K. Le Guin" }),
  ).toBeFocused();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("catalog completion refreshes the shelf and an open book list", async ({
  page,
}) => {
  const state = await fixture(page, true);
  state.startRefresh();
  await page.clock.install();
  await page.goto("/following");
  const author = page.getByRole("article", {
    name: "Ursula K. Le Guin",
    exact: true,
  });
  await author.getByRole("button", { name: "2 books in catalog" }).click();
  const expanded = author.getByRole("region", {
    name: "Ursula K. Le Guin books",
  });
  await expect(
    expanded.getByRole("link", { name: "A Wizard of Earthsea", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Coming up", exact: true }),
  ).toHaveCount(0);
  state.completeRefresh();
  await page.clock.fastForward(5000);
  await expect(
    author.getByRole("button", { name: "3 books in catalog" }),
  ).toBeVisible();
  await expect(
    expanded.getByRole("link", {
      name: "Sample future publication",
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("region", { name: "Coming up", exact: true })
      .getByRole("link", { name: "Sample future publication", exact: true }),
  ).toBeVisible();
});

for (const tab of ["authors", "releases"]) {
  test(
    "hidden pending follows refresh the " + tab + " view",
    async ({ page }) => {
      const state = await fixture(page, true);
      state.startRefresh();
      await page.clock.install();
      await page.goto("/following?filter=upcoming&tab=" + tab);
      if (tab === "authors") {
        await expect(
          page.getByRole("heading", { name: "No matching follows" }),
        ).toBeVisible();
      } else {
        await expect(
          page.getByText("No announced releases from your follows.", {
            exact: false,
          }),
        ).toBeVisible();
      }
      state.completeRefresh();
      await page.clock.fastForward(5000);
      const region = page.getByRole("region", {
        name: tab === "authors" ? "Coming up" : "Followed releases",
        exact: true,
      });
      await expect(
        region.getByRole("link", {
          name: "Sample future publication",
          exact: true,
        }),
      ).toBeVisible();
      if (tab === "authors") {
        await expect(
          page.getByRole("button", { name: "3 books in catalog" }),
        ).toBeVisible();
      }
    },
  );
}
