import { expect, test } from "../fixtures";

test("a collection is reviewed before transfer, selects the requested book, and submits exact files", async ({
  page,
}, info) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const work = {
    id: "work-1",
    title: "Angels & Demons",
    authors: ["Dan Brown"],
    versions: [],
    availability: { owned: false, ebook: false, audio: false, stale: false },
  };
  const profile = {
    id: null,
    generation: 0,
    name: "Balanced",
    preferences: {
      source_order: ["mam"],
      criteria: ["format", "source", "seeders"],
      preferred_narrators: [],
      ebook_formats: ["epub"],
      audio_formats: ["m4b"],
      blocked_formats: [],
      search_series: true,
      prefer_series_packs: true,
    },
    overrides: {},
    origins: {},
    scope_origins: {},
  };
  const release = {
    source: "mam",
    source_id: "60963",
    title: "Dan Brown collection",
    raw_title: "Dan Brown collection",
    authors: ["Dan Brown"],
    medium: "audio",
    language: "en",
    formats: ["m4b"],
    tags: [],
    narrators: [],
    series: [],
    seeders: 100,
    size_bytes: 2000,
    description: "Includes five books",
    freeleech: true,
  };
  const entries = ["Angels & Demons", "Digital Fortress"].map((title, i) => ({
    id: `entry-${i}`,
    title,
    match: "exact",
    evidence: [],
    recordings: [],
    files: [`Pack/${title}.m4b`],
    candidates: [
      {
        id: `candidate-${i}`,
        title,
        authors: ["Dan Brown"],
        external_id: String(i + 1),
        cover_url: null,
        series: [],
        owned: false,
      },
    ],
  }));
  let inspected = false;
  const downloads: unknown[] = [];
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "admin",
          username: "reader",
          display_name: "Reader",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/catalog/works/work-1") data = work;
    else if (path === "/api/metadata/works/work-1")
      data = {
        sources: [],
        conflicts: [],
        fields: {},
        versions: [],
        versions_total: 0,
      };
    else if (path.endsWith("/reader-match")) data = { book: null };
    else if (path === "/api/acquisition/preferences/personal")
      data = { effective: profile.preferences };
    else if (path.includes("/quick-add/latest/")) data = null;
    else if (path.endsWith("/source-searches/latest"))
      data = {
        id: "search-1",
        work_id: work.id,
        query: work.title,
        medium: "all",
        offset: 0,
        status: "completed",
        message: "Search complete",
        stale_identity: false,
        expires_at: new Date(Date.now() + 600000).toISOString(),
        profile,
        sources: [
          {
            key: "mam",
            name: "MAM",
            state: "completed",
            count: 1,
            message: "1 result",
          },
        ],
        items: [
          {
            id: "result-1",
            possible_collection: true,
            release,
            current_connection: true,
            assessment: { blocked: [], review: [], explanation: [] },
            expires_at: new Date(Date.now() + 600000).toISOString(),
          },
        ],
      };
    else if (path.endsWith("/contents"))
      data = {
        review_id: "review-1",
        revision: "a".repeat(64),
        title: release.title,
        possible_collection: true,
        requested_title: work.title,
        entries: entries.map((e) => ({
          ...e,
          files: inspected ? e.files : [],
        })),
        files: inspected
          ? entries.map((e) => ({ path: e.files[0], size_bytes: 1000 }))
          : [],
        warnings: [],
        artifact_id: inspected ? "artifact-1" : null,
        bibliography_count: 18,
        excluded: [],
        series_coverage: [],
      };
    else if (path.endsWith("/artifact")) {
      inspected = true;
      data = { id: "artifact-1" };
    } else if (path === "/api/collection-reviews/review-1/download") {
      downloads.push(route.request().postDataJSON());
      data = {
        attempt_id: "attempt-1",
        message: "Queued one collection transfer for 1 reviewed book",
      };
    } else if (path === "/api/acquisition/profiles") data = [profile];
    else if (path === "/api/metadata/account") data = { enabled: false };
    else if (path.includes("/source-searches/") && path.endsWith("/download"))
      throw new Error("Collection bypassed review");
    await route.fulfill({ json: data });
  });
  await page.goto("/books/work-1?tab=sources");
  await page
    .getByRole("button", { name: "Download Dan Brown collection", exact: true })
    .click();
  const dialog = page.getByRole("dialog", { name: "Review collection" });
  await expect(
    dialog.getByRole("heading", { name: "Dan Brown collection", exact: true }),
  ).toBeVisible();
  expect(downloads).toEqual([]);
  await dialog
    .getByRole("button", { name: "Review downloadable files" })
    .click();
  await expect(
    dialog.getByText("1 books selected", { exact: false }),
  ).toBeVisible();
  expect(downloads).toEqual([]);
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(
      dialog.getByRole("button", {
        name: "Download selected books",
        exact: true,
      }),
    ).toBeVisible();
    expect(
      await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth + 1),
    ).toBeTruthy();
    await page.screenshot({
      path: info.outputPath(`collection-review-${width}.png`),
    });
  }
  await dialog
    .getByRole("button", { name: "Download selected books", exact: true })
    .click();
  await expect(
    dialog.getByText("Queued one collection transfer for 1 reviewed book", {
      exact: false,
    }),
  ).toBeVisible();
  expect(downloads).toEqual([
    {
      revision: "a".repeat(64),
      choices: [
        {
          entry_id: "entry-0",
          candidate_id: "candidate-0",
          paths: ["Pack/Angels & Demons.m4b"],
        },
      ],
      download_all_files: false,
    },
  ]);
  expect(errors).toEqual([]);
});
