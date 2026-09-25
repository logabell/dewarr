import { expect, test } from "../fixtures";

test("requests show transfer telemetry, review actions and counts in compact rows", async ({
  page,
}, testInfo) => {
  // This journey checks request presentation; authentication is covered by foundation.
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/catalog/cover-image") return route.fallback();
    let data: unknown = { items: [], total: 0 };
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "admin",
          username: "reader",
          display_name: "Test Reader",
          onboarding_status: "completed",
          permissions: [],
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/request-quotas/me")
      data = { bypass: true, windows: [], pending_remaining: null };
    else if (path.includes("/acquisition/preferences/"))
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
  const request = (id: string, title: string, target: object) => ({
    id,
    work_id: id,
    work_title: title,
    authors: ["Andy Weir"],
    owner_name: "Test Reader",
    can_open_book: true,
    can_withdraw: false,
    can_decide: false,
    approval_status: "approved",
    specification: { mode: "audio" },
    reasons: [
      {
        id: `${id}-reason`,
        active: true,
        label: "Your request",
        approval_status: "approved",
      },
    ],
    targets: [
      {
        slot: "audio",
        state: "wanted",
        next_action: "downloads",
        message: "Acquisition pending; check download activity",
        ...target,
      },
    ],
  });
  let progress = 0.42;
  await page.route("**/api/requests/counts", (route) =>
    route.fulfill({
      json: { pending: 1, downloading: 1, review: 1, active: 3 },
    }),
  );
  await page.route(/\/api\/requests(?:\?|$)/, (route) => {
    const items = [
      request("download", "Project Hail Mary", {
        attempt_id: "transfer",
        attempt_state: "downloading",
        progress,
        download_speed: 2516582,
        eta_seconds: 185,
      }),
      request("review", "The Martian", {
        attempt_id: "held",
        attempt_state: "complete",
        progress: 1,
        needs_review: true,
        import_state: "held",
        inspection_id: "inspection",
        can_recheck: true,
        review_message: "Files need review",
      }),
      {
        ...request("fulfilled", "Artemis", {
          state: "satisfied",
          next_action: "book",
          attempt_id: "finished",
          attempt_state: "complete",
          progress: 1,
          can_recheck: true,
          can_cancel: true,
        }),
        can_withdraw: true,
        cover_url: "https://assets.hardcover.app/editions/artemis.jpg",
      },
    ];
    return route.fulfill({
      json: {
        items:
          new URL(route.request().url()).searchParams.get("status") === "review"
            ? [items[1]]
            : items,
        total: 3,
        offset: 0,
        limit: 10,
      },
    });
  });
  await page.goto("/requests");
  const table = page.getByRole("table", {
    name: "Book requests and download activity",
  });
  await expect(
    table.getByRole("columnheader", { name: "Speed", exact: true }),
  ).toBeVisible();
  const downloading = table.getByRole("rowgroup", {
    name: "Project Hail Mary request",
  });
  await expect(downloading).toContainText("42%");
  await expect(downloading).toContainText("2.4 MiB/s");
  await expect(downloading).toContainText("4m");
  const review = table.getByRole("rowgroup", { name: "The Martian request" });
  const fulfilled = table.getByRole("rowgroup", { name: "Artemis request" });
  await expect(fulfilled).toContainText("In library");
  await expect(fulfilled.getByRole("button", { name: "Recheck" })).toHaveCount(
    0,
  );
  await expect(
    fulfilled.getByRole("button", { name: "Cancel download" }),
  ).toHaveCount(0);
  await expect(
    fulfilled.getByRole("button", { name: "Actions for Artemis" }),
  ).toHaveCount(0);
  await expect(
    fulfilled.getByRole("button", { name: "Details for Artemis" }),
  ).toBeVisible();
  const cover = fulfilled.locator(".request-cover img");
  await expect(cover).toHaveAttribute(
    "src",
    "/api/catalog/cover-image?url=https%3A%2F%2Fassets.hardcover.app%2Feditions%2Fartemis.jpg",
  );
  await expect(cover).toHaveJSProperty("naturalWidth", 200);
  expect((await cover.boundingBox())!.height).toBeLessThan(50);
  await expect(review).toContainText("Needs review");
  await expect(
    review.getByRole("link", { name: "Review files" }),
  ).toHaveAttribute("href", "/organization/inspections?inspection=inspection");
  await expect(table).not.toContainText("Acquisition pending");
  await expect(
    page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: "Requests", exact: true }),
  ).toContainText("3");
  const filters = page.getByRole("navigation", { name: "Request filters" });
  await expect(
    filters.getByRole("link", { name: "Review", exact: true }),
  ).toContainText("1");
  expect((await downloading.boundingBox())!.height).toBeLessThan(110);
  progress = 0.67;
  await expect(downloading).toContainText("67%", { timeout: 10000 });
  await page.screenshot({
    path: testInfo.outputPath("request-ledger-desktop.png"),
    fullPage: true,
  });
  await review.getByRole("button", { name: "Details for The Martian" }).click();
  await expect(review).toContainText("Files need review");
  await page.setViewportSize({ width: 390, height: 844 });
  const layout = await page.evaluate(() => ({
    width: innerWidth,
    doc: document.documentElement.scrollWidth,
    nodes: [
      ...document.querySelectorAll(
        "body, main, .requests-page, .requests-board, .request-ledger-scroll, .page-tabs-bar, .page-tabs",
      ),
    ].map((e) => ({
      tag: e.className,
      w: e.clientWidth,
      sw: e.scrollWidth,
      rect: e.getBoundingClientRect().toJSON(),
    })),
  }));
  expect(layout.doc, JSON.stringify(layout)).toBeLessThanOrEqual(layout.width);
  await page.screenshot({
    path: testInfo.outputPath("request-ledger-mobile.png"),
    fullPage: true,
  });
});
