import { expect, test } from "../fixtures";

test("reuse an arbitrary library mount for both formats", async ({
  page,
}, testInfo) => {
  page.on("pageerror", (error) => {
    throw error;
  });
  const ebook = {
    id: "ebook-destination",
    root_key: "library-ebook",
    medium: "ebook",
    library_id: "mixed",
    backend_path: "/remote-collection",
    local_path: "/Reading Room",
    staging_path: "/Reading Room/.book-search-staging",
    mode: "copy",
    enabled: true,
    revision: "one",
    configured: true,
    publication_available: false,
    server_kind: "audiobookshelf",
  };
  const destinations: any[] = [ebook];
  let saved: any;
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "user",
          role: "admin",
          display_name: "Reader",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/integrations")
      data = [
        {
          id: "abs",
          name: "Audiobookshelf",
          kind: "audiobookshelf",
          enabled: true,
          status: "connected",
          library_count: 1,
          book_count: 250,
          base_url: "http://library:13378",
        },
      ];
    else if (path === "/api/library/libraries")
      data = [{ id: "mixed", name: "My Collection", accessible: true }];
    else if (path.startsWith("/api/acquisition/preferences/"))
      data = { effective: {}, inherited: {}, overrides: {}, revision: "one" };
    else if (path === "/api/organization/destinations")
      data = destinations.map((item) => ({
        ...item,
        shared_root: destinations.length === 2,
      }));
    else if (path === "/api/organization/library-folders")
      data = [
        {
          library_id: "mixed",
          library_name: "My Collection",
          server_kind: "audiobookshelf",
          ebooks_allowed: true,
          audio_allowed: true,
          folders: ["/remote-collection"],
        },
      ];
    else if (path.endsWith("/automatic-import"))
      data = {
        enabled: false,
        requested_enabled: true,
        can_enable: false,
        generation: 0,
      };
    else if (path === "/api/organization/library-folders/audio") {
      saved = route.request().postDataJSON();
      const audio = {
        ...ebook,
        ...saved,
        id: "audio-destination",
        root_key: "library-audio",
        medium: "audio",
        shared_root: true,
      };
      destinations.push(audio);
      data = audio;
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#libraries");
  await page.getByRole("button", { name: "Choose audiobooks folder" }).click();
  const dialog = page.getByRole("dialog", { name: "Choose audiobooks folder" });
  await dialog
    .getByRole("button", { name: "Use the same folder as ebooks" })
    .click();
  await expect(dialog.getByLabel("Custom library path")).toHaveValue(
    "/Reading Room",
  );
  await expect(
    dialog.getByRole("status").filter({ hasText: "Shared library" }),
  ).toContainText("Shared library · Ebooks + Audiobooks");
  await page.screenshot({
    path: testInfo.outputPath("shared-picker-desktop.png"),
    fullPage: false,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect
    .poll(() => dialog.evaluate((node) => node.scrollWidth <= node.clientWidth))
    .toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("shared-picker-mobile.png"),
    fullPage: false,
  });
  await dialog
    .getByRole("button", { name: "Save folder", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(saved).toMatchObject({
    library_id: "mixed",
    local_path: "/Reading Room",
    backend_path: "/remote-collection",
  });
  await expect(
    page.getByText("Shared · Ebooks + Audiobooks", { exact: true }),
  ).toHaveCount(2);
  const card = page.getByRole("region", { name: "Audiobooks destination" });
  const storage = card.getByText("Temporary import storage", { exact: true });
  await storage.focus();
  await expect(storage).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(
    card.getByText("/Reading Room/.book-search-staging", { exact: true }),
  ).toBeVisible();
  // Dewarr currently has one dark theme; native controls must stay legible
  // even when the operating system requests light colors.
  for (const theme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: theme });
    await expect(page.locator("html")).toHaveCSS("color-scheme", "dark");
    await expect
      .poll(() =>
        page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      )
      .toBe(true);
    await page.screenshot({
      path: testInfo.outputPath(`shared-folders-mobile-system-${theme}.png`),
      fullPage: true,
    });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.screenshot({
      path: testInfo.outputPath(`shared-folders-desktop-system-${theme}.png`),
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
  }
  const longPath = "/" + "Long-library-mount-name-".repeat(7);
  for (const destination of destinations) {
    destination.local_path = longPath;
    destination.staging_path = `${longPath}/.book-search-staging`;
  }
  await page.reload();
  await card.getByText("Temporary import storage", { exact: true }).click();
  await expect(
    card.getByText(`${longPath}/.book-search-staging`, { exact: true }),
  ).toBeVisible();
  await expect
    .poll(() =>
      page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    )
    .toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("shared-folders-long-path-mobile.png"),
    fullPage: true,
  });
});

test("manual review carries its shared destination from naming into import", async ({
  page,
}) => {
  page.on("pageerror", (error) => {
    throw error;
  });
  const destinations = [false, true].map((shared, index) => ({
    id: `destination-${index}`,
    medium: "ebook",
    root_key: `collection-${index}`,
    backend_path: shared ? "/mixed" : "/separate",
    shared_root: shared,
    enabled: true,
    publication_available: true,
    revision: "route-revision",
    mode: "copy",
  }));
  const group = {
    key: "group",
    title: "First Harbor",
    medium: "ebook",
    authors: ["Alex Morgan"],
    narrators: [],
    files: [{ path: "book.epub", role: "media" }],
  };
  let frozen: any;
  let imported: any;
  let changedDestination = false;
  const plan = () => ({
    id: "saved",
    inspection_id: "review",
    revision: "plan-revision",
    document: {
      destinations: frozen.destinations,
      shared_media: ["ebook"],
      profile: { layout: "conventional" },
      version_revisions: { edition: "edition-revision" },
      plan: {
        expected_items: 1,
        held_items: 0,
        items: [
          {
            group_id: "group",
            work_id: "book",
            title: "First Harbor",
            medium: "ebook",
            state: "ready",
            files: [
              {
                source: "book.epub",
                destination:
                  "Alex Morgan/First Harbor (Ebook)/First Harbor.epub",
              },
            ],
          },
        ],
      },
    },
  });
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "user",
          role: "admin",
          display_name: "Reader",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/organization/destinations") data = destinations;
    else if (path === "/api/organization/settings")
      data = { revision: "profile-revision" };
    else if (path === "/api/organization/inspections/review")
      data = {
        id: "review",
        state: "ready",
        relative_path: "Completed book",
        snapshot: { revision: "inspection-revision", files: group.files },
      };
    else if (path.endsWith("/grouping"))
      data = {
        revision: "grouping-revision",
        content: { groups: [group], excluded: [] },
      };
    else if (path.endsWith("/matches"))
      data = {
        items: [
          {
            group_key: "group",
            status: "matched",
            selected_version_id: "edition",
            revision: "match-revision",
            message: "Matched book",
            evidence: { issues: [] },
            candidates: [
              {
                work_id: "book",
                version_id: "edition",
                title: "First Harbor",
                medium: "ebook",
                authors: group.authors,
                narrators: [],
                reasons: [],
                conflicts: [],
              },
            ],
          },
        ],
        total: 1,
      };
    else if (path === "/api/metadata/works/book")
      data = {
        versions: [
          {
            id: "edition",
            medium: "ebook",
            title: "First Harbor",
            narrators: [],
          },
        ],
        versions_total: 1,
      };
    else if (path === "/api/organization/inspections/review/plans") {
      if (!changedDestination) {
        changedDestination = true;
        destinations[1].id = "destination-new";
        return route.fulfill({
          status: 409,
          json: { detail: "Choose an enabled ebook destination" },
        });
      }
      frozen = route.request().postDataJSON();
      data = plan();
    } else if (path === "/api/organization/plans/saved") data = plan();
    else if (
      path === "/api/organization/plans/saved/imports" &&
      route.request().method() === "POST"
    ) {
      imported = route.request().postDataJSON();
      data = { id: "run", entries: [] };
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/organization/inspections?inspection=review");
  await page
    .getByRole("button", { name: "Use matched edition", exact: true })
    .click();
  await page
    .getByRole("checkbox", { name: /These files contain the complete book/ })
    .check();
  await page
    .getByRole("combobox", { name: "Ebook destination" })
    .selectOption("destination-1");
  await page
    .getByRole("button", { name: "Save import plan", exact: true })
    .click();
  await expect(
    page.getByText("Choose an enabled ebook destination", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Refresh review settings", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Use matched edition", exact: true })
    .click();
  await page
    .getByRole("checkbox", { name: /These files contain the complete book/ })
    .check();
  await page
    .getByRole("combobox", { name: "Ebook destination" })
    .selectOption("destination-new");
  await page
    .getByRole("button", { name: "Save import plan", exact: true })
    .click();
  const saved = page.getByRole("article", { name: "Saved import plan" });
  await expect(saved).toContainText("First Harbor (Ebook)/First Harbor.epub");
  expect(frozen.destinations).toEqual({ ebook: "destination-new" });
  await expect(
    saved.getByRole("combobox", { name: "Ebook destination" }),
  ).toHaveValue("destination-new");
  await saved
    .getByRole("button", { name: "Import resolved books", exact: true })
    .click();
  await expect
    .poll(() => imported)
    .toMatchObject({
      destinations: {
        ebook: { id: "destination-new", revision: "route-revision" },
      },
    });
});
