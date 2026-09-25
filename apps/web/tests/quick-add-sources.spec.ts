import { expect, test } from "./fixtures";

test("Quick add follows defaults and format overrides; sources provide compact rows and complete details", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const work = {
    id: "work-1",
    title: "Project Hail Mary",
    authors: ["Andy Weir"],
    description: "A lone astronaut must save the earth.",
    versions: [],
    availability: { owned: false, ebook: false, audio: false, stale: false },
  };
  const profile = {
    id: null,
    generation: 0,
    name: "Balanced",
    preferences: {
      desired_media: "both",
      preferred_medium: "audio",
      language: null,
      abridged: null,
      required_narrators: [],
      standalone: false,
      ebook_library_id: null,
      audio_library_id: null,
      search_series: true,
      prefer_series_packs: true,
      ebook_formats: ["epub", "pdf", "azw3", "mobi", "azw", "cbz", "cbr"],
      audio_formats: ["m4b", "mp3", "flac", "aac", "ogg", "opus"],
      source_order: ["mam", "prowlarr"],
      criteria: ["format", "source", "seeders"],
      preferred_narrators: [],
      blocked_formats: [],
      maximum_bytes: null,
    },
    overrides: {},
    origins: {},
    effective_revision: null,
    base_effective_revision: null,
    list_overrides: null,
    request_overrides: null,
    scope_origins: {},
  };
  const release = {
    source: "mam",
    source_id: "720129",
    title: work.title,
    raw_title: work.title,
    authors: work.authors,
    narrators: ["Ray Porter"],
    medium: "audio",
    formats: ["m4b"],
    size_bytes: 924634316,
    seeders: 7534,
    snatches: 25000,
    leechers: 2,
    freeleech: true,
    vip: true,
    tags: ["Science Fiction", "Unabridged"],
    series: [],
    language: "en",
    uploaded_at: "2026-09-01",
    description: "Search description",
    media_info: "General\nFormat: MPEG-4\nDuration: 16 hours",
    observed_at: new Date().toISOString(),
  };
  const search = {
    id: "search-1",
    work_id: work.id,
    request_id: "request-1",
    query: work.title,
    medium: "all",
    offset: 0,
    status: "completed",
    message: "Search complete",
    stale_identity: false,
    expires_at: new Date(Date.now() + 25 * 60_000).toISOString(),
    profile,
    sources: [
      {
        key: "mam",
        name: "MAM",
        state: "completed",
        count: 2,
        message: "2 results",
      },
    ],
    items: [
      {
        id: "result-1",
        release: { ...release, freeleech: false, personal_freeleech: false },
        current_connection: true,
        expires_at: new Date(Date.now() + 25 * 60_000).toISOString(),
        assessment: {
          blocked: [],
          review: [],
          explanation: ["Preferred M4B format"],
        },
      },
      {
        id: "result-2",
        release: {
          ...release,
          source_id: "720130",
          medium: "ebook",
          narrators: [],
          formats: ["epub"],
          seeders: 2754,
          size_bytes: 9856614,
          vip: false,
        },
        current_connection: true,
        expires_at: new Date(Date.now() + 25 * 60_000).toISOString(),
        assessment: {
          blocked: ["Fixture blocked release"],
          review: [],
          explanation: [],
        },
      },
    ],
  };
  const posted: Record<string, unknown>[] = [];
  let receipt: unknown = null;
  const releaseDownloads: string[] = [];
  const wedgeChoices: Array<string | null> = [];
  let held = false;
  let refreshes = 0;
  let savedDownload: Record<string, unknown> | null = null;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = {};
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
    else if (path === "/api/acquisition/preferences/personal")
      data = { effective: profile.preferences };
    else if (path === "/api/requests/quick-add") {
      posted.push(route.request().postDataJSON());
      receipt = {
        id: "quick-1",
        status: "completed",
        message: "Preferred downloads queued",
      };
      data = receipt;
    } else if (path.includes("/quick-add/latest/")) data = receipt;
    else if (path === "/api/requests/request-1")
      data = { id: "request-1", release_policy: profile };
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
    else if (path === "/api/acquisition/profiles") data = [profile];
    else if (path === "/api/library/libraries") data = [];
    else if (path === "/api/sources/prowlarr/indexers") data = [];
    else if (path === "/api/catalog/works/work-1/source-searches") {
      refreshes++;
      search.id = `search-refreshed-${refreshes}`;
      search.expires_at = new Date(Date.now() + 60 * 60_000).toISOString();
      search.items.forEach((item) => {
        item.expires_at = search.expires_at;
        item.current_connection = true;
      });
      data = search;
    } else if (path.endsWith("/source-searches/latest"))
      data = {
        ...search,
        items: search.items.map((item) => ({
          ...item,
          download: item.id === "result-1" ? savedDownload : null,
        })),
      };
    else if (path === "/api/sources/mam/releases/720129")
      data = {
        ...release,
        description:
          "Full MAM description\nA mission beyond the solar system.\n<script>untrusted()</script>",
      };
    else if (path === "/api/sources/mam/releases/720130")
      data = search.items[1].release;
    else if (path.endsWith("/download") && path.includes("/results/")) {
      const url = new URL(route.request().url());
      releaseDownloads.push(url.pathname);
      wedgeChoices.push(url.searchParams.get("use_wedge"));
      data = {
        id: "selected-1",
        status: "queued",
        message: "Preparing selected release",
      };
    } else if (path === "/api/acquisition/automatic-selections/selected-1") {
      savedDownload = {
        state: held ? "failed" : "queued",
        message: held
          ? "The release language does not match this request"
          : "Selected release download started",
        request_id: "request-1",
        operation_id: "selected-1",
        reasons: held
          ? ["The release language does not match this request"]
          : [],
        prevent_download: !held,
      };
      data = {
        id: "selected-1",
        status: held ? "held" : "completed",
        message: held
          ? "No eligible release found within this page and inspection budget; review candidate reasons or refresh results"
          : "Selected release download started",
        download_id: held ? null : "download-1",
        decisions: held
          ? [
              {
                result_id: "result-1",
                reasons: ["The release language does not match this request"],
              },
            ]
          : [],
      };
    } else if (path.endsWith("/artifact")) data = { id: "artifact-1" };
    else if (path.endsWith("/torrent"))
      return route.fulfill({
        contentType: "application/x-bittorrent",
        headers: {
          "Content-Disposition": 'attachment; filename="release.torrent"',
        },
        body: "d4:infod4:name7:fixtureee",
      });
    else if (path === "/api/catalog/works") data = { items: [], total: 0 };
    else if (path.includes("/group"))
      data = { items: [], groups: [], members: [] };
    else if (path.endsWith("/versions")) data = [];
    else if (path.endsWith("/reader-details"))
      data = { authors: [], reviews: [] };
    else if (path === "/api/requests") data = { items: [], total: 0 };
    await route.fulfill({ json: data });
  });
  await page.goto("/books/work-1");
  await page.getByRole("button", { name: "Quick add", exact: true }).click();
  await expect(page.getByText("Preferred downloads queued")).toBeVisible();
  expect(posted[0].specification).toEqual({});
  await page.getByLabel("Quick add format", { exact: true }).click();
  const split = page.locator(".quick-add-split");
  const menuOptions = page.locator(".quick-add-options");
  const splitBox = (await split.boundingBox())!;
  const menuBox = (await menuOptions.boundingBox())!;
  expect(Math.abs(menuBox.x - splitBox.x)).toBeLessThanOrEqual(1);
  expect(menuBox.y).toBeGreaterThan(splitBox.y + splitBox.height);
  await expect(menuOptions.getByRole("button").first()).toHaveCSS(
    "justify-content",
    "flex-start",
  );
  await expect(menuOptions.getByRole("button").first()).toHaveCSS(
    "height",
    "30px",
  );
  await page.screenshot({
    path: testInfo.outputPath("quick-add-menu-desktop.png"),
  });

  await page.getByRole("button", { name: "Ebook", exact: true }).click();
  await expect.poll(() => posted.length).toBe(2);
  expect(posted[1].specification).toEqual({ mode: "ebook" });
  await page.getByLabel("Quick add format", { exact: true }).click();
  await page.getByRole("button", { name: "Audiobook", exact: true }).click();
  await expect.poll(() => posted.length).toBe(3);
  expect(posted[2].specification).toEqual({ mode: "audio" });
  await page.getByLabel("Quick add format", { exact: true }).click();
  await expect(page.locator(".quick-add-options button")).toHaveText([
    "Both",
    "Ebook",
    "Audiobook",
  ]);
  await page.getByRole("button", { name: "Both", exact: true }).click();
  await expect.poll(() => posted.length).toBe(4);
  expect(posted[3].specification).toEqual({ mode: "both" });
  receipt = {
    id: "quick-1",
    status: "running",
    message: "Searching MAM",
    source_checks: [],
  };
  await page.reload();
  const quickStatus = page.getByRole("region", { name: "Quick add progress" });
  await expect(quickStatus).toContainText("Finding your download");
  await expect(quickStatus).toContainText(
    "Finding the best match using your saved preferences.",
  );
  await expect(
    page.getByRole("button", { name: "Adding…", exact: true }),
  ).toBeDisabled();
  receipt = {
    id: "quick-1",
    status: "held",
    request_id: "request-1",
    message:
      "Audiobook: Request added. No automatic release was found. MAM: No eligible release found within this page and inspection budget",
    source_checks: [
      {
        slot: "audio",
        source: "MAM",
        status: "held",
        candidates: 7,
        inspected: 5,
        message:
          "No ready torrent download route. Check your torrent client and import destination.",
        reasons: ["No ready torrent download route"],
      },
      {
        slot: "audio",
        source: "Prowlarr indexers",
        status: "held",
        candidates: 8,
        inspected: 0,
        message: "The source must corroborate the catalog title and author",
        reasons: ["The source must corroborate the catalog title and author"],
      },
    ],
  };
  await expect(quickStatus).toContainText("Request needs attention");
  await expect(quickStatus).not.toContainText("inspection budget");
  await expect(quickStatus).not.toContainText("Source checks");
  await expect(quickStatus).not.toContainText("inspected");
  await expect(quickStatus).toContainText(
    "Check your download client and library folder settings",
  );
  await expect(
    quickStatus.getByRole("link", { name: "Check download settings" }),
  ).toHaveAttribute("href", "/settings#downloaders");
  await expect(
    quickStatus.getByRole("link", { name: "Review sources" }),
  ).toHaveAttribute(
    "href",
    "/books/work-1?tab=sources&request=request-1&slot=audio",
  );
  for (const width of [1236, 390]) {
    await page.setViewportSize({ width, height: 930 });
    const box = (await quickStatus.boundingBox())!;
    expect(box.width).toBeGreaterThan(220);
    expect(box.x + box.width).toBeLessThanOrEqual(width);
    await page.screenshot({
      path: testInfo.outputPath(`quick-add-held-${width}.png`),
      fullPage: true,
    });
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await quickStatus.getByRole("link", { name: "Review sources" }).click();
  await expect(
    page.getByRole("heading", { name: "Prepare the best release" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Prepare best eligible release" }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Candidate decisions", { exact: false }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Choose a verified torrent downloader", { exact: false }),
  ).toHaveCount(0);
  await expect(page).toHaveURL(/tab=sources/);
  const table = page.getByRole("table");
  await expect(table.getByRole("row")).toHaveCount(3);
  for (const name of [
    "Title",
    "Author(s)",
    "Narrators",
    "Size",
    "Format",
    "Seeds",
    "Tags",
  ])
    await expect(
      table.getByRole("columnheader", { name, exact: true }),
    ).toBeVisible();
  await expect(table).toContainText("881.8 MiB");
  await expect(table).toContainText("7,534");
  await expect(table).toContainText("Freeleech");
  await expect(table).toContainText("VIP");
  const sourceDownload = table.getByRole("button", {
    name: "Download Project Hail Mary",
    exact: true,
  });
  const wedge = table.getByRole("checkbox", { name: "Use a Freeleech wedge" });
  await expect(wedge).toHaveCount(1);
  await expect(wedge).toBeEnabled();
  await wedge.check();
  await expect(sourceDownload.nth(1)).toBeDisabled();
  await sourceDownload.first().click();
  await expect(
    table.locator(".source-download-status").first(),
  ).toHaveAttribute("title", "Selected release download started");
  await expect(sourceDownload.first()).toBeDisabled();
  expect(releaseDownloads).toEqual([
    "/api/source-searches/search-1/results/result-1/download",
  ]);
  expect(wedgeChoices).toEqual(["true"]);
  await page.getByLabel("Sort this view").click();
  await page
    .getByRole("listbox", { name: "Sort this view" })
    .getByRole("option", { name: "Smallest download", exact: true })
    .click();
  await expect(table.locator("tbody tr").first()).toContainText("EPUB");
  await page.getByLabel("Sort this view").selectOption("profile");
  await page.screenshot({
    path: testInfo.outputPath("sources-desktop.png"),
    fullPage: true,
  });
  const details = page
    .getByRole("button", { name: "Details for Project Hail Mary" })
    .first();
  await details.click();
  const dialog = page.getByRole("dialog", { name: "Release details" });
  await expect(dialog).toContainText("Full MAM description");
  await expect(dialog.locator(".release-prose p")).toHaveCount(3);
  await expect(
    page.getByRole("heading", { name: "Download sources", exact: true }),
  ).toHaveCount(0);
  await dialog.getByRole("tab", { name: "Media info", exact: true }).click();
  await expect(dialog).toContainText("16 hours");
  await expect(dialog.locator("dl")).toContainText("MPEG-4");
  await dialog.screenshot({
    path: testInfo.outputPath("release-media-desktop.png"),
  });
  await page.keyboard.press("ArrowRight");
  await expect(
    dialog.getByRole("tab", { name: "Details", exact: true }),
  ).toBeFocused();
  await dialog.getByRole("tab", { name: "Description", exact: true }).click();
  await expect(dialog).toContainText("Science Fiction");
  await page.screenshot({
    path: testInfo.outputPath("release-details-desktop.png"),
    fullPage: true,
  });
  await expect(
    dialog.getByRole("button", { name: "Inspect this release" }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "Save torrent" }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "Download Project Hail Mary" }),
  ).toBeDisabled();
  expect(releaseDownloads).toHaveLength(1);
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(details).toBeFocused();
  await page
    .getByRole("button", { name: "Details for Project Hail Mary" })
    .nth(1)
    .click();
  await expect(
    dialog.getByRole("button", { name: "Download Project Hail Mary" }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("sources-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await table.locator(".release-title-button").first().click();
  await expect(dialog).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("release-details-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.keyboard.press("Escape");
  held = true;
  savedDownload = null;
  search.items[0].release.freeleech = true;
  await page.reload();
  await details.click();
  await dialog
    .getByRole("button", { name: "Download Project Hail Mary" })
    .click();
  await expect(dialog).toContainText("Download not started");
  expect(releaseDownloads).toEqual([
    "/api/source-searches/search-1/results/result-1/download",
    "/api/source-searches/search-1/results/result-1/download",
  ]);
  expect(wedgeChoices).toEqual(["true", null]);
  await page.keyboard.press("Escape");
  const feedback = table.locator(".source-download-status").first();
  await expect(feedback).toContainText("Download not started");
  await expect(feedback).toHaveAttribute("data-tone", "error");
  for (const width of [1236, 390]) {
    await page.setViewportSize({ width, height: 930 });
    expect((await feedback.boundingBox())!.height).toBeLessThanOrEqual(32);
    await table.screenshot({
      path: testInfo.outputPath(`held-download-${width}.png`),
    });
  }
  await expect(feedback).toHaveAttribute(
    "title",
    /The release language does not match this request/,
  );
  savedDownload = {
    state: "failed",
    message: "This release needs a qBittorrent connection",
    request_id: "request-1",
    operation_id: "selected-1",
    reasons: ["This release needs a qBittorrent connection"],
    prevent_download: false,
  };
  await page.clock.install();
  await page.clock.fastForward(31_000);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("offline"));
    window.dispatchEvent(new Event("online"));
  });
  await expect(feedback).toHaveAttribute("title", /qBittorrent connection/);
  // A withdrawal clears the saved receipt while the source table stays mounted.
  savedDownload = null;
  await page.clock.fastForward(31_000);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("offline"));
    window.dispatchEvent(new Event("online"));
  });
  await expect(table.locator(".source-download-status")).toHaveCount(0);
  await expect(sourceDownload.first()).toBeEnabled();
  const downloadCount = releaseDownloads.length;
  for (const [state, label] of [
    ["queued", "Download queued"],
    ["downloading", "Downloading"],
    ["downloaded", "Downloaded"],
    ["imported", "In library"],
  ]) {
    savedDownload = {
      state,
      message: "Saved release status",
      request_id: "request-1",
      operation_id: "selected-1",
      attempt_id: "download-1",
      progress: 0.42,
      reasons: [],
      prevent_download: true,
    };
    await page.reload();
    await expect(feedback).toContainText(label);
    await expect(sourceDownload.first()).toBeDisabled();
    await expect(sourceDownload).toHaveCount(1); // Only the blocked release retains its download button.
    await expect(table.locator(".source-download-status")).toHaveCount(1);
    await expect(feedback.getByRole("progressbar")).toHaveCount(0);
    if (state === "downloading")
      await expect(feedback.locator(".source-download-spinner")).toHaveCount(1);
    if (state === "imported") {
      await expect(feedback).toHaveAttribute("data-tone", "success");
      await expect(feedback).toHaveAttribute(
        "href",
        "/requests#request-request-1",
      );
      await page.setViewportSize({ width: 1236, height: 930 });
      await table.screenshot({
        path: testInfo.outputPath("imported-release.png"),
      });
    }
  }
  await page.setViewportSize({ width: 390, height: 844 });
  expect(releaseDownloads).toHaveLength(downloadCount);
  savedDownload = null;
  const expiry = new Date(
    (await page.evaluate(() => Date.now())) + 60_000,
  ).toISOString();
  search.expires_at = expiry;
  search.items.forEach((item) => {
    item.expires_at = expiry;
  });
  await page.reload();
  await expect(sourceDownload.first()).toBeEnabled();
  await page.clock.fastForward(61_000);
  await expect(sourceDownload.first()).toBeDisabled();
  await expect(
    page.getByText("Search results expired", { exact: true }),
  ).toBeVisible();
  await expect(sourceDownload.first()).toHaveAttribute(
    "title",
    "Search result expired. Refresh results to download.",
  );
  await page
    .getByRole("checkbox", { name: "Hide blocked or expired results" })
    .check();
  await expect(table).toHaveCount(0);
  await page
    .getByRole("button", { name: "Refresh results", exact: true })
    .click();
  await expect.poll(() => refreshes).toBe(1);
  await expect(sourceDownload.first()).toBeEnabled();
  await expect(
    page.getByText("Search results expired", { exact: true }),
  ).toHaveCount(0);
  search.items[0].current_connection = false;
  await page.reload();
  await expect(table).toContainText("Source settings changed");
  await expect(table).not.toContainText("Search result expired");
  await expect(sourceDownload.first()).toBeDisabled();
  await page
    .getByRole("button", { name: "Refresh results", exact: true })
    .click();
  await expect(sourceDownload.first()).toBeEnabled();
  await page.screenshot({
    path: testInfo.outputPath("sources-refreshed.png"),
    fullPage: true,
  });
  search.items = Array.from({ length: 55 }, (_, index) => ({
    ...search.items[0],
    id: `result-${index}`,
    release: {
      ...release,
      title: `Release ${index}`,
      narrators: [`Narrator ${index}`],
      seeders: index === 54 ? 90000 : index,
    },
  }));
  await page.reload();
  await expect(table.locator("tbody tr")).toHaveCount(50);
  await page.locator(".book-sources .infinite-scroll").scrollIntoViewIfNeeded();
  await expect(table.locator("tbody tr")).toHaveCount(55);
  await page.getByLabel("Filter title, author or narrator").fill("Narrator 54");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table).toContainText("Release 54");
  await page
    .getByRole("button", { name: "Reset result view", exact: true })
    .click();
  await page.getByLabel("Sort this view").selectOption("seeds");
  await expect(table.locator("tbody tr").first()).toContainText("Release 54");
  let imported = 0;
  receipt = null;
  await page.route("**/api/metadata/books/hardcover/9010", (route) =>
    route.fulfill({
      json: {
        book: {
          provider: "hardcover",
          external_id: "9010",
          title: work.title,
          authors: work.authors,
          editions: [],
        },
        work: null,
      },
    }),
  );
  await page.route("**/api/metadata/books/hardcover/9010/import", (route) => {
    imported++;
    return route.fulfill({ json: work });
  });
  await page.goto("/discover/books/hardcover/9010");
  await page.getByLabel("Quick add format", { exact: true }).click();
  const mobileSplit = (await split.boundingBox())!;
  const mobileMenu = (await menuOptions.boundingBox())!;
  expect(Math.abs(mobileMenu.x - mobileSplit.x)).toBeLessThanOrEqual(1);
  expect(mobileMenu.x + mobileMenu.width).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: testInfo.outputPath("quick-add-menu-mobile.png"),
    fullPage: true,
  });
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: "Quick add", exact: true }).click();
  await expect.poll(() => posted.length).toBe(5);
  expect(imported).toBe(1);
  expect(posted[4]).toEqual({ work_id: work.id, specification: {} });
  await expect(page.getByText("Preferred downloads queued")).toBeVisible();
  await page
    .getByRole("button", { name: "Search sources", exact: true })
    .click();
  await expect(page).toHaveURL(/books\/work-1\?tab=sources/);
  expect(errors).toEqual([]);
});
