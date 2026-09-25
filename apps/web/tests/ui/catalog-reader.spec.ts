import { expect, test } from "../fixtures";

for (const unlinked of [false, true]) {
  test(`catalog reader details with ${unlinked ? "no provider binding" : "accepted source"}`, async ({
    page,
  }, testInfo) => {
    const id = "00000000-0000-0000-0000-000000000042";
    const writes: string[] = [];
    const errors: string[] = [];
    const downloadQueries: string[] = [];
    let owned = true;
    let role = "member";
    let detailsFail = false;
    let matchFail = false;
    let lockedDescription = false;
    const title =
      "The Quiet Harbor: A Lighthouse Keeper’s Journey Through Forgotten Coastal Towns, Hidden Histories, and the Extraordinary Stories That Bring Us Home Again";
    const synopsis =
      "A lighthouse keeper returns to a coastal town, carrying a secret that could change its future.";
    const art = `data:image/svg+xml,${encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="400" height="600"><rect width="400" height="600" fill="#23404e"/><circle cx="270" cy="165" r="90" fill="#dcbd7c"/><path d="M0 390L400 230V600H0Z" fill="#142a39"/><text x="35" y="450" font-family="serif" font-size="38" fill="#f3dfae">THE QUIET</text><text x="35" y="498" font-family="serif" font-size="38" fill="#f3dfae">HARBOR</text></svg>')}`;
    const work = () => ({
      id,
      title,
      authors: ["Morgan Vale"],
      description: null,
      publication_year: 2024,
      language: "English",
      cover_url: art,
      provisional: false,
      availability: {
        owned,
        ebook: owned,
        audio: false,
        stale: false,
        in_collection: false,
      },
    });
    const source = {
      id: "source",
      provider: "hardcover",
      external_id: "42",
      title: "The Quiet Harbor",
      fetched_at: "2026-09-19T00:00:00Z",
      cover_url: art,
      editions_more: false,
      book: {
        description: synopsis,
        subjects: ["Coastal fiction"],
        editions: unlinked
          ? [
              {
                external_id: "hc-edition",
                title: "Hardcover paperback",
                medium: "print",
                narrators: [],
                language: "English",
                publication_year: 2024,
                publisher: "Coast Press",
              },
            ]
          : [],
      },
      series: [{ external_id: "7", name: "Coastal stories", position: "1" }],
    };
    await page.route("**/api/**", (route) => {
      const url = new URL(route.request().url());
      if (route.request().method() !== "GET") writes.push(url.pathname);
      let data: unknown = { items: [], total: 0 };
      if (url.pathname === "/api/auth/me")
        data = {
          user: {
            id: "reader",
            role,
            username: "reader",
            display_name: "Reader",
          },
          csrf_token: "test",
        };
      else if (url.pathname === "/api/setup/onboarding")
        data = { status: "completed" };
      else if (url.pathname === `/api/catalog/works/${id}`) data = work();
      else if (url.pathname === "/api/metadata/books/hardcover/42")
        data = { book: { description: null, subjects: ["Coastal fiction"] } };
      else if (url.pathname.endsWith("/cover"))
        return route.fulfill({ status: 404 });
      else if (url.pathname === `/api/metadata/works/${id}`)
        data = {
          sources: unlinked ? [] : [source],
          fields: lockedDescription ? { description: { locked: true } } : {},
          versions_total: 2,
          versions: [
            {
              id: "edition",
              medium: "ebook",
              title: "The Quiet Harbor",
              narrators: [],
              language: "English",
              publication_year: 2024,
              owned,
              needs_review: false,
              identifiers: {},
            },
          ],
          cover_choices: [],
          enrichment: null,
        };
      else if (url.pathname.endsWith("/reader-match")) {
        if (matchFail)
          return route.fulfill({ status: 404, json: { detail: "Not found" } });
        data = {
          status: "matched",
          book: {
            ...source.book,
            provider: "hardcover",
            external_id: "42",
            title,
            authors: ["Morgan Vale"],
            series: source.series,
            cover_url: art,
          },
        };
      } else if (url.pathname.endsWith("/reader-details")) {
        if (detailsFail)
          return route.fulfill({
            status: 503,
            json: { detail: "Reviews temporarily unavailable" },
          });
        data = {
          external_id: "42",
          rating: 4.35,
          ratings_count: 824,
          pages: 352,
          release_date: "2024-05-21",
          authors: [
            {
              external_id: "author",
              name: "Morgan Vale",
              bio: "Morgan writes stories about the coast.",
            },
          ],
          reviews: [],
        };
      } else if (url.pathname === "/api/library/assets")
        data = {
          total: owned ? 1 : 0,
          items: owned
            ? [
                {
                  id: "copy",
                  title: "The Quiet Harbor",
                  authors: ["Morgan Vale"],
                  work_ids: [id],
                  library_name: "Home library",
                  medium: "ebook",
                  narrators: [],
                  formats: ["epub"],
                  full_content: true,
                  state: "present",
                  open_url: "https://library.example/item/42",
                  files: [
                    {
                      path: "/books/Morgan Vale/The Quiet Harbor/The Quiet Harbor.epub",
                      format: "epub",
                      size: 3460300,
                    },
                  ],
                  last_seen_at: "2026-09-19T00:00:00Z",
                },
              ]
            : [],
        };
      else if (url.pathname === "/api/acquisition/downloads") {
        downloadQueries.push(url.searchParams.get("work_id") || "");
        data = {
          total: owned ? 1 : 0,
          items: owned
            ? [
                {
                  id: "download",
                  release_title: "The Quiet Harbor — EPUB",
                  source: "mam",
                  state: "complete",
                  message:
                    "Download complete; import confirmed in your library.",
                  created_at: "2026-09-18T12:00:00Z",
                  progress: 1,
                },
              ]
            : [],
        };
      } else if (url.pathname.startsWith("/api/discovery/related"))
        data = {
          title: "Related books",
          status: "ready",
          items: [],
          attribution: "Catalog picks",
        };
      else if (url.pathname === "/api/lists/page")
        data = { items: [{ id: "list", name: "Weekend reads" }], total: 1 };
      else if (url.pathname === "/api/acquisition/profiles") data = [];
      if (
        new URL(route.request().url()).pathname.includes(
          "/acquisition/preferences/",
        )
      )
        data = { effective: { desired_media: "both" } };
      return route.fulfill({ json: data });
    });
    await page.goto(`/books/${id}`);
    await expect(page.locator(".reader-hero h1")).toHaveAccessibleName(title);
    await expect(page.locator(".reader-hero h1")).toHaveText(
      "The Quiet Harbor",
    );
    await expect(page.locator(".reader-subtitle")).toHaveCSS(
      "-webkit-line-clamp",
      "2",
    );
    await page.getByRole("button", { name: "Show full title" }).click();
    await expect(
      page.getByRole("button", { name: "Show less" }),
    ).toHaveAttribute("aria-expanded", "true");
    await page.getByRole("button", { name: "Show less" }).click();
    await expect(page.locator(".reader-hero")).toContainText("4.35");
    await expect(page.locator(".reader-facts")).toContainText("352");
    await expect(page.locator(".reader-facts")).toContainText("May 21, 2024");
    await expect(page.getByText(synopsis, { exact: true })).toBeVisible();
    await expect(
      page.getByRole("link", { name: "View on Goodreads" }),
    ).toBeVisible();
    await expect(
      page.getByRole("region", { name: "Library copies" }),
    ).toHaveCount(0);
    await page.screenshot({
      path: testInfo.outputPath("catalog-overview-desktop.png"),
      fullPage: true,
    });
    await page
      .getByRole("link", { name: "View library copies", exact: true })
      .click();
    await expect(
      page.getByRole("tab", { name: "Library copies" }),
    ).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("table")).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "About this book" }),
    ).toHaveCount(0);
    await page.screenshot({
      path: testInfo.outputPath("catalog-library-desktop.png"),
      fullPage: true,
    });
    await page.getByRole("button", { name: "1 file · Location" }).click();
    const files = page.getByRole("dialog", { name: "Files & location" });
    await expect(
      files.getByText(
        "/books/Morgan Vale/The Quiet Harbor/The Quiet Harbor.epub",
        { exact: true },
      ),
    ).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(files).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "1 file · Location" }),
    ).toBeFocused();
    await page.getByRole("tab", { name: "Downloads", exact: true }).click();
    await expect(page.getByRole("table")).toContainText("MyAnonamouse");
    expect(downloadQueries).toEqual([id]);
    expect(writes).toEqual([]);
    await page.getByRole("tab", { name: "Editions", exact: true }).click();
    await expect(page.getByRole("table").first()).toContainText("English");
    if (unlinked)
      await expect(
        page.getByRole("region", { name: "Hardcover editions" }),
      ).toContainText("Coast Press");
    await expect(
      page.getByText("Advanced metadata options", { exact: true }),
    ).toHaveCount(0);
    await page
      .getByRole("button", { name: "Add to reading list", exact: true })
      .click();
    const list = page.getByRole("dialog", {
      name: "Add to reading list",
      exact: true,
    });
    await list.getByRole("radio", { name: "Weekend reads" }).click();
    await list
      .getByRole("button", { name: "Add to list", exact: true })
      .click();
    await expect(list.getByText("Added to your list.")).toBeVisible();
    expect(writes).toEqual(["/api/lists/list/entries"]);
    await page.keyboard.press("Escape");
    await expect(list).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Add to reading list", exact: true }),
    ).toBeFocused();
    await page.getByRole("tab", { name: "Overview", exact: true }).click();
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
      path: testInfo.outputPath("catalog-overview-mobile.png"),
      fullPage: true,
    });
    await page
      .getByRole("tab", { name: "Library copies", exact: true })
      .click();
    await page.screenshot({
      path: testInfo.outputPath("catalog-library-mobile.png"),
      fullPage: true,
    });
    const dimensions = await page.evaluate(() => ({
      width: innerWidth,
      scroll: document.documentElement.scrollWidth,
      overflowing: [...document.querySelectorAll("body *")]
        .filter(
          (el) =>
            el.getBoundingClientRect().right > innerWidth &&
            getComputedStyle(el).position !== "fixed",
        )
        .map((el) => ({
          tag: el.tagName,
          cls: el.className,
          right: el.getBoundingClientRect().right,
        }))
        .slice(0, 15),
    }));
    expect(dimensions.scroll, JSON.stringify(dimensions)).toBe(390);
    await page
      .getByRole("link", { name: "Book metadata", exact: true })
      .click();
    await expect(
      page.getByText("Advanced metadata options", { exact: true }),
    ).toBeVisible();
    await page.getByRole("tab", { name: "Reviews", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Reader reviews", exact: true }),
    ).toBeVisible();
    role = "viewer";
    owned = false;
    detailsFail = true;
    await page.reload();
    await expect(page.locator(".reader-hero")).toContainText(
      "Saved in your catalog",
    );
    await expect(
      page.getByRole("button", { name: "Add to reading list", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByText("Reviews temporarily unavailable"),
    ).toBeVisible();
    await page.getByRole("tab", { name: "Overview", exact: true }).click();
    await expect(page.getByText(synopsis, { exact: true })).toBeVisible();
    lockedDescription = true;
    await page.reload();
    await expect(page.getByText(synopsis, { exact: true })).toHaveCount(0);
    await expect(
      page.getByText("No synopsis is available for this book yet."),
    ).toBeVisible();
    if (unlinked) {
      matchFail = true;
      detailsFail = false;
      lockedDescription = false;
      await page.reload();
      const status = page.getByRole("status", { name: "Book details status" });
      await expect(status).toContainText("Restart or update the app server");
      await expect(status).toContainText(
        "Your library copies and files are still available",
      );
      await expect(page.getByText("Not found", { exact: true })).toHaveCount(0);
      await expect(
        page.getByText("Some book details are unavailable."),
      ).toHaveCount(0);
      await page.screenshot({
        path: testInfo.outputPath("metadata-error-mobile.png"),
        fullPage: true,
      });
      matchFail = false;
      await status.getByRole("button", { name: "Retry details" }).click();
      await expect(status).toHaveCount(0);
      await expect(page.getByText(synopsis, { exact: true })).toBeVisible();
    }
    expect(errors).toEqual([]);
  });
}
