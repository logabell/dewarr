import { expect, test } from "../fixtures";

for (const scenario of ["single", "alternate", "unmapped"]) {
  const alternate = scenario !== "single";
  const unmapped = scenario === "unmapped";
  test(`series requests lead to collection review with ${scenario} recording choices`, async ({
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
      evidence: [{ basis: "description" }],
      recordings:
        alternate && i === 0
          ? [
              { narrator_claim: "First Reader" },
              { narrator_claim: "Second Reader" },
            ]
          : [],
      recording_options:
        alternate && i === 0
          ? [
              {
                id: "0",
                claims: { narrator_claim: "First Reader" },
                files: unmapped ? [] : [`Pack/${title}/First Reader.m4b`],
              },
              {
                id: "1",
                claims: { narrator_claim: "Second Reader" },
                files: [`Pack/${title}/Second Reader.m4b`],
              },
            ]
          : [],
      files:
        alternate && i === 0
          ? [
              `Pack/${title}/First Reader.m4b`,
              `Pack/${title}/Second Reader.m4b`,
            ]
          : [`Pack/${title}.m4b`],
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
    let accepted = false;
    const seriesCommands: unknown[] = [];
    const seriesRequest = () => ({
      id: "series-request",
      status: accepted ? "completed" : "preview",
      message: accepted ? "Requests saved" : "Review the selected book",
      records: [
        {
          work_id: work.id,
          title: work.title,
          position: "1",
          warnings: [],
          targets: [{ slot: "audio", state: accepted ? "pending" : "wanted" }],
        },
      ],
      omitted: [],
      specification: { mode: "audio" },
      release_policy: profile,
      counts: {
        satisfied: 0,
        wanted: accepted ? 0 : 1,
        pending: accepted ? 1 : 0,
        cancelled: 0,
      },
      receipt: accepted
        ? [{ work_id: work.id, request_id: "request-1" }]
        : null,
    });
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
      else if (path === "/api/catalog/series/hardcover/1084")
        data = {
          external_id: "1084",
          name: "Robert Langdon",
          authors: ["Dan Brown"],
          projection_version: 2,
          generation: 1,
          fetched_at: "2026-09-28T00:00:00Z",
          status: "completed",
          message: "",
          books: 1,
          total: 1,
          raw_total: 1,
          owned: 0,
          ebook: 0,
          audio: 0,
          supplements: 0,
          planned: 0,
          items: [
            {
              membership_id: "member-1",
              work,
              position: "1",
              publication: "published",
              category: "main",
              compilation: false,
              partial: false,
              merged_record: false,
              ambiguous_position: false,
            },
          ],
        };
      else if (path.endsWith("/main-books")) data = null;
      else if (path === "/api/catalog/series/hardcover/1084/requests")
        data = { items: [], total: 0 };
      else if (path.endsWith("/requests/preview")) {
        seriesCommands.push(route.request().postDataJSON());
        data = seriesRequest();
      } else if (path.endsWith("/requests/series-request/submit")) {
        accepted = true;
        data = seriesRequest();
      } else if (path.endsWith("/requests/series-request"))
        data = seriesRequest();
      else if (path === "/api/acquisition/selections/options")
        data = { downloaders: [], destinations: [] };
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
            ? entries.flatMap((e) =>
                e.files.map((path) => ({ path, size_bytes: 1000 })),
              )
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
    await page.goto("/series/hardcover/1084?tab=requests");
    await page
      .getByRole("button", { name: "Select missing", exact: true })
      .click();
    await page.getByRole("button", { name: "Audiobook", exact: true }).click();
    await page
      .getByRole("button", { name: "Review 1 selected book", exact: true })
      .click();
    await expect(page).toHaveURL(/request=series-request/);
    await page.reload();
    await page
      .getByRole("button", { name: "Save series requests", exact: true })
      .click();
    expect(seriesCommands).toEqual([
      expect.objectContaining({
        work_ids: [work.id],
        specification: { mode: "audio" },
        scope: "selected",
      }),
    ]);
    await page
      .getByRole("link", { name: "Review sources", exact: true })
      .click();
    await expect(page).toHaveURL(/\/books\/work-1\?tab=sources$/);
    await page
      .getByRole("button", {
        name: "Download Dan Brown collection",
        exact: true,
      })
      .click();
    const dialog = page.getByRole("dialog", { name: "Review collection" });
    await expect(
      dialog.getByRole("heading", {
        name: "Dan Brown collection",
        exact: true,
      }),
    ).toBeVisible();
    expect(downloads).toEqual([]);
    await dialog
      .getByRole("button", { name: "Review downloadable files" })
      .click();
    if (alternate) {
      await expect(
        dialog.getByText("0 books selected", { exact: false }),
      ).toBeVisible();
      const book = dialog.locator("article").filter({
        has: page.getByRole("heading", { name: /Angels & Demons/ }),
      });
      await expect(
        book.getByRole("checkbox", { name: /Select this book/ }),
      ).toBeDisabled();
      await dialog
        .getByLabel("Recording for Angels & Demons")
        .selectOption("0");
      if (unmapped) {
        await dialog
          .getByRole("button", { name: /^Download all files/ })
          .click();
        await expect.poll(() => downloads.length).toBe(1);
        expect(downloads).toEqual([
          {
            revision: "a".repeat(64),
            download_all_files: true,
            choices: [
              {
                entry_id: "entry-1",
                candidate_id: "candidate-1",
                paths: ["Pack/Digital Fortress.m4b"],
              },
            ],
          },
        ]);
        expect(errors).toEqual([]);
        return;
      }
      await book.getByRole("checkbox", { name: /Select this book/ }).check();
    }
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
            ...(alternate ? { recording_id: "0" } : {}),
            paths: [
              alternate
                ? "Pack/Angels & Demons/First Reader.m4b"
                : "Pack/Angels & Demons.m4b",
            ],
          },
        ],
        download_all_files: false,
      },
    ]);
    expect(errors).toEqual([]);
  });
}
