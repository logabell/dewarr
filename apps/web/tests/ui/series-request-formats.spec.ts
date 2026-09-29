import { expect, test, type Page } from "../fixtures";

async function mockSeries(page: Page, allEbooks = false, paged = false) {
  const previews: Record<string, unknown>[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const titles = [
    "Twilight",
    "New Moon",
    "Eclipse",
    "Breaking Dawn",
    "Midnight Sun",
  ];
  const entries = titles.map((title, i) => ({
    membership_id: `member-${i}`,
    external_id: String(i),
    position: String(i + 1),
    publication: "published",
    category: "main",
    compilation: false,
    partial: false,
    merged_record: false,
    ambiguous_position: false,
    work: {
      id: `work-${i}`,
      title,
      authors: ["Stephenie Meyer"],
      versions: [],
      cover_url: `https://example.com/${i}.jpg`,
      availability: {
        owned: allEbooks || i < 4,
        ebook: allEbooks || i < 4,
        audio: false,
        stale: false,
      },
    },
  }));
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (route.request().resourceType() === "image") return route.fallback();
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "admin",
          display_name: "Reader",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/acquisition/profiles") {
      data = [
        {
          id: null,
          name: "My defaults",
          preferences: {
            desired_media: "both",
            source_order: [],
            criteria: [],
            preferred_narrators: [],
            ebook_formats: [],
            audio_formats: [],
            blocked_formats: [],
          },
          overrides: {},
          origins: {},
        },
      ];
    } else if (path === "/api/catalog/series/hardcover/5451") {
      const offset = Number(url.searchParams.get("offset") || 0);
      data = {
        external_id: "5451",
        name: "The Twilight Saga",
        authors: ["Stephenie Meyer"],
        projection_version: 2,
        generation: 1,
        fetched_at: "2026-09-28T00:00:00Z",
        status: "completed",
        message: "",
        books: 5,
        total: 5,
        raw_total: 5,
        supplements: 0,
        planned: 0,
        owned: allEbooks ? 5 : 4,
        ebook: allEbooks ? 5 : 4,
        audio: 0,
        items: paged ? entries.slice(offset, offset + 3) : entries,
      };
    } else if (path.endsWith("/main-books")) data = null;
    else if (path.endsWith("/requests")) data = { items: [], total: 0 };
    else if (path.endsWith("/requests/preview")) {
      previews.push(route.request().postDataJSON());
      // Keep the draft open; this test verifies the submitted command, not acquisition.
      return route.fulfill({
        status: 409,
        json: { detail: "Preview captured" },
      });
    } else if (path === "/api/acquisition/selections/options")
      data = {
        downloaders: [
          {
            id: "qbit",
            name: "qBittorrent",
            source_key: "mam",
            ready: true,
            generation: 1,
            protocol: "torrent",
          },
        ],
        destinations: ["ebook", "audio"].map((medium) => ({
          id: medium,
          name: medium === "ebook" ? "E-books" : "Audiobooks",
          medium,
          source_key: "mam",
          ready: true,
          automatic_import_ready: true,
          revision: 1,
        })),
      };
    return route.fulfill({ json: data });
  });
  return { previews, errors };
}

test("request missing uses personal formats, counts each gap, and preserves manual choices", async ({
  page,
}, info) => {
  const { previews, errors } = await mockSeries(page, false, true);
  await page.goto("/series/hardcover/5451");
  await page
    .getByRole("button", { name: "Request missing books", exact: true })
    .click();
  const composer = page.getByRole("region", {
    name: "Series requests",
    exact: true,
  });
  const checked = page.locator(".series-book-row input:checked");
  await expect(checked).toHaveCount(5);
  await expect(
    composer.getByText("My defaults · Both formats", { exact: true }),
  ).toBeVisible();
  await expect(composer.getByText("1 ebook", { exact: true })).toBeVisible();
  await expect(
    composer.getByText("5 audiobooks", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Ebook", exact: true }).click();
  await expect(checked).toHaveCount(1);
  await expect(
    page.getByRole("checkbox", { name: "Select Midnight Sun", exact: true }),
  ).toBeChecked();
  await page.getByRole("button", { name: "Audiobook", exact: true }).click();
  await expect(checked).toHaveCount(5);
  await page.getByRole("button", { name: "Either", exact: true }).click();
  await expect(checked).toHaveCount(1);
  await expect(
    composer.getByText("1 book · either format", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Both", exact: true }).click();
  await expect(checked).toHaveCount(5);
  await page
    .getByRole("checkbox", { name: "Select Twilight", exact: true })
    .uncheck();
  await expect(checked).toHaveCount(4);
  await expect(
    composer.getByText("4 audiobooks", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("checkbox", { name: "Download automatically", exact: true })
    .check();
  await expect(
    composer.getByText("Download settings · Ready", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByLabel("Downloader", { exact: true }),
  ).not.toBeVisible();
  await expect(checked).toHaveCount(4);
  await page.getByRole("button", { name: "My defaults", exact: true }).click();
  await expect(checked).toHaveCount(5);
  await page.screenshot({
    path: info.outputPath("series-request-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await composer.scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("series-request-mobile.png") });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page
    .getByRole("button", { name: "Review request", exact: true })
    .click();
  await expect.poll(() => previews.length).toBe(1);
  expect(previews[0].work_ids).toEqual([
    "work-0",
    "work-1",
    "work-2",
    "work-3",
    "work-4",
  ]);
  expect(previews[0].specification).toEqual({});
  expect(previews[0].automatic).toMatchObject({
    routes: {
      ebook: { destination_id: "ebook" },
      audio: { destination_id: "audio" },
    },
  });
  expect(errors).toEqual([]);
});

test("an ebook-complete series can request all missing audiobooks and honor discovery format links", async ({
  page,
}) => {
  await mockSeries(page, true);
  await page.goto("/series/hardcover/5451");
  await page
    .getByRole("button", { name: "Request missing books", exact: true })
    .click();
  await expect(page.locator(".series-book-row input:checked")).toHaveCount(5);
  await expect(page.getByText("0 ebooks", { exact: true })).toBeVisible();
  await expect(page.getByText("5 audiobooks", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Ebook", exact: true }).click();
  await expect(page.locator(".series-book-row input:checked")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Review request", exact: true }),
  ).toBeDisabled();
  await page.goto("/series/hardcover/5451?tab=requests&gaps=1&medium=audio");
  await expect(
    page.getByRole("button", { name: "Audiobook", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".series-book-row input:checked")).toHaveCount(5);
});
