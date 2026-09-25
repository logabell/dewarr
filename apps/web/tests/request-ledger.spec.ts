import { expect, test } from "./fixtures";

test("requests show transfer telemetry, review actions and counts in compact rows", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  await page.getByLabel("Username", { exact: true }).fill("reader");
  await page
    .getByLabel("Password", { exact: true })
    .fill("browser test password");
  if (await page.getByLabel("Your name").isVisible()) {
    await page.getByLabel("Your name").fill("Test Reader");
    await page.getByRole("button", { name: "Create administrator" }).click();
    await page
      .getByRole("button", { name: "Finish later", exact: true })
      .click();
  } else
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeVisible();
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
    ];
    return route.fulfill({
      json: {
        items:
          new URL(route.request().url()).searchParams.get("status") === "review"
            ? [items[1]]
            : items,
        total: 2,
        offset: 0,
        limit: 10,
      },
    });
  });
  await page.getByRole("link", { name: "Requests", exact: true }).click();
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
