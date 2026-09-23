import { expect, test } from "./fixtures";

const asset = (id: string, title: string, readIssues: string[]) => ({
  id,
  library_id: "library",
  library_name: "Audiobooks",
  server_kind: "audiobookshelf",
  external_id: id,
  medium: "audio",
  state: "present",
  title,
  authors: readIssues.includes("authors") ? [] : ["Becky Chambers"],
  narrators: ["Rachel Dulude"],
  series: [],
  work_ids: [],
  works: [],
  match_status: "needs-review",
  match_revision: "rev-3",
  read_issues: readIssues,
  files: [{ path: `/audiobooks/${title}/part1.m4b` }],
  open_url: `https://abs.example.test/item/${id}`,
});

test("library review links unmatched items to Dewarr and Hardcover books", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const matches: { asset: string; body: unknown }[] = [];
  let imported = false;
  const items = [
    { kind: "asset", asset: asset("a1", "Untagged Folder", ["title"]) },
    { kind: "asset", asset: asset("a2", "The Long Way", []) },
    {
      kind: "read-issue",
      read_issue: {
        id: "r1",
        library_id: "library",
        library_name: "Audiobooks",
        server_kind: "audiobookshelf",
        external_id: "r1",
        title: "Broken Rip",
        authors: [],
        path: "/audiobooks/Broken Rip",
        reasons: ["Unlisted media file"],
        last_seen_at: "2026-09-23T00:00:00Z",
        open_url: "https://abs.example.test/item/r1",
      },
    },
  ];
  const work = {
    id: "00000000-0000-0000-0000-000000000007",
    title: "A Closed and Common Orbit",
    authors: ["Becky Chambers"],
    availability: { owned: false, audio: false, ebook: false, stale: false },
  };
  const book = {
    provider: "hardcover",
    external_id: "42",
    title: "The Long Way to a Small, Angry Planet",
    authors: ["Becky Chambers"],
    publication_year: 2014,
    description: "A tunneling ship takes on a long job.",
    editions: [],
    series: [],
    subjects: [],
  };
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith("/api/")) return route.continue();
    const method = route.request().method();
    let data: unknown = { items: [], total: 0 };
    if (path === "/api/auth/me")
      data = {
        csrf_token: "test",
        user: {
          id: "admin",
          role: "admin",
          username: "admin",
          display_name: "Reader",
          onboarding_status: "complete",
        },
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/library/review/summary") {
      const assets = items.filter((item) => item.asset);
      data = {
        total: items.length,
        needs_matching: assets.length,
        read_issues: items.filter(
          (item) => item.read_issue || item.asset!.read_issues.length,
        ).length,
        reasons: [
          { reason: "title", count: 1 },
          { reason: "Unlisted media file", count: 1 },
        ].filter(({ reason }) =>
          items.some((item) =>
            (item.asset?.read_issues ?? item.read_issue!.reasons).includes(
              reason,
            ),
          ),
        ),
      };
    } else if (path === "/api/library/review") {
      const kind = url.searchParams.get("kind");
      const shown = items.filter((item) =>
        kind === "needs-matching"
          ? item.asset
          : kind === "read-issue"
            ? item.read_issue || item.asset!.read_issues.length
            : true,
      );
      data = { items: shown, total: shown.length };
    } else if (path === "/api/catalog/works")
      data = { items: [work], total: 1 };
    else if (path.endsWith("/match") && method === "POST") {
      const id = path.split("/").at(-2)!;
      matches.push({ asset: id, body: route.request().postDataJSON() });
      items.splice(
        items.findIndex((item) => item.asset?.id === id),
        1,
      );
      data = { ok: true };
    } else if (path === "/api/metadata/search")
      data = {
        provider: "hardcover",
        items: [book],
        known_works: {},
        page: 1,
        has_more: false,
      };
    else if (path === "/api/metadata/books/hardcover/42") data = { book };
    else if (path === "/api/metadata/books/hardcover/42/import") {
      imported = true;
      data = { ...work, id: "00000000-0000-0000-0000-000000000042" };
    }
    await route.fulfill({ json: data });
  });

  await page.goto("/review");
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  await expect(nav.getByRole("link", { name: /^Review/ })).toContainText("3");
  await expect(
    page.getByRole("heading", { name: "Library review" }),
  ).toBeVisible();
  const filters = page.getByRole("navigation", { name: "Review filter" });
  await expect(filters.getByRole("link", { name: /Everything/ })).toContainText(
    "3",
  );
  await expect(
    filters.getByRole("link", { name: /Needs matching/ }),
  ).toContainText("2");
  await expect(page.getByLabel("Most common problems")).toContainText(
    "Missing title",
  );
  const cards = page.locator(".review-card");
  await expect(cards).toHaveCount(3);
  await page.screenshot({
    path: "test-results/library-review-desktop.png",
    fullPage: true,
  });

  await filters.getByRole("link", { name: /Couldn't read fully/ }).click();
  await expect(page).toHaveURL(/kind=read-issue/);
  await expect(cards).toHaveCount(2);
  const broken = page.getByRole("article", { name: "Broken Rip" });
  await expect(broken).toContainText("Couldn't read files");
  await expect(
    broken.getByRole("link", { name: /Open in Audiobookshelf/ }),
  ).toHaveAttribute("href", "https://abs.example.test/item/r1");
  await broken.getByRole("button", { name: "See details" }).click();
  const detail = page.getByRole("dialog", { name: "Review library item" });
  await expect(detail).toContainText("What Audiobookshelf reports");
  await expect(detail).toContainText("/audiobooks/Broken Rip");
  await expect(detail).toContainText("run a library sync");
  await detail
    .getByRole("button", { name: "Close review library item" })
    .click();
  await expect(detail).toHaveCount(0);

  await filters.getByRole("link", { name: /Everything/ }).click();
  await page
    .getByRole("article", { name: "Untagged Folder" })
    .getByRole("button", { name: "Find the book" })
    .click();
  await expect(detail).toContainText("Missing title");
  await detail.getByRole("combobox", { name: "Book" }).selectOption(work.id);
  await page.screenshot({ path: "test-results/library-review-dialog.png" });
  await detail.getByRole("button", { name: "Confirm match" }).click();
  await expect(detail).toHaveCount(0);
  expect(matches[0]).toEqual({
    asset: "a1",
    body: { work_id: work.id, expected_revision: "rev-3" },
  });
  await expect(page.getByRole("status")).toContainText(
    "Linked “Untagged Folder”",
  );
  await expect(cards).toHaveCount(2);
  await expect(nav.getByRole("link", { name: /^Review/ })).toContainText("2");

  await page
    .getByRole("article", { name: "The Long Way" })
    .getByRole("button", { name: "Find the book" })
    .click();
  await detail.getByRole("link", { name: "On Hardcover" }).click();
  await expect(page).toHaveURL(/source=hardcover/);
  await expect(
    detail.getByRole("button", {
      name: /The Long Way to a Small, Angry Planet/,
    }),
  ).toBeVisible();
  await page.screenshot({ path: "test-results/library-review-hardcover.png" });
  await expect(detail.getByLabel("Title, author or identifier")).toHaveValue(
    "The Long Way Becky Chambers",
  );
  await detail
    .getByRole("button", { name: /The Long Way to a Small, Angry Planet/ })
    .click();
  await detail.getByRole("button", { name: "Use this book" }).click();
  await expect(detail).toHaveCount(0);
  expect(imported).toBe(true);
  expect(matches[1]).toEqual({
    asset: "a2",
    body: {
      work_id: "00000000-0000-0000-0000-000000000042",
      expected_revision: "rev-3",
    },
  });
  await expect(cards).toHaveCount(1);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "test-results/library-review-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});
