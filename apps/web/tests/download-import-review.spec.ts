import { expect, test } from "./fixtures";

test("linked downloads resume their saved import instead of asking for catalog matching", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("Username", { exact: true }).fill("reader");
  await page
    .getByLabel("Password", { exact: true })
    .fill("browser test password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("navigation", { name: "Main navigation" }),
  ).toBeVisible();
  let imported = false;
  let retries = 0;
  const plan = {
    id: "saved-plan",
    inspection_id: "linked",
    revision: "revision",
    document: {
      profile: { layout: "conventional" },
      version_revisions: { edition: "revision" },
      plan: {
        items: [
          {
            group_id: "group",
            work_id: "book",
            title: "Project Hail Mary",
            medium: "audio",
            state: "ready",
            folder: "Andy Weir/Project Hail Mary",
            files: [
              {
                source: "Project Hail Mary.m4b",
                destination: "Project Hail Mary - Ray Porter.m4b",
              },
            ],
          },
        ],
      },
    },
  };
  await page.route("**/api/organization/inspections/linked", (route) =>
    route.fulfill({
      json: {
        id: "linked",
        state: "ready",
        relative_path: "[M4B] Andy Weir - Project Hail Mary",
        message: "Files inspected",
        plan_id: "saved-plan",
        download: {
          attempt_id: "attempt",
          work_id: "book",
          title: "Project Hail Mary",
          authors: ["Andy Weir"],
          cover_url: null,
          medium: "audio",
          destination: "/media/audiobooks",
          mode: "hardlink",
          state: imported ? "complete" : "held",
          message: imported
            ? "Imported and available in your library."
            : "The worker cannot write to the library. Check folder permissions. (EACCES)",
          can_retry: false,
        },
        snapshot: {
          files: [
            {
              path: "Project Hail Mary.m4b",
              state: "inspected",
              medium: "audio",
            },
            { path: "Project Hail Mary.cue", state: "held", medium: null },
            { path: "Project Hail Mary.jpg", state: "held", medium: null },
            { path: "Project Hail Mary.nfo", state: "held", medium: null },
          ],
        },
      },
    }),
  );
  await page.route("**/api/organization/inspections?*", (route) =>
    route.fulfill({ json: [] }),
  );
  await page.route("**/api/organization/download-roots", (route) =>
    route.fulfill({ json: ["completed-downloads"] }),
  );
  await page.route("**/api/organization/plans/saved-plan", (route) =>
    route.fulfill({ json: plan }),
  );
  await page.route("**/api/organization/destinations", (route) =>
    route.fulfill({ json: [] }),
  );
  const run = () => [
    {
      id: "run",
      plan_id: "saved-plan",
      entries: [
        {
          id: "entry",
          group_id: "group",
          state: imported ? "confirmed" : "held",
          message: imported
            ? "Available in your library"
            : "The worker cannot write to the library. Check folder permissions. (EACCES)",
          can_retry: !imported,
          can_cancel: !imported,
          published_at: imported ? new Date().toISOString() : null,
        },
      ],
    },
  ];
  await page.route("**/api/organization/plans/saved-plan/imports", (route) =>
    route.fulfill({ json: run() }),
  );
  await page.route(
    "**/api/organization/imports/run/entries/entry/retry",
    (route) => {
      imported = true;
      retries += 1;
      return route.fulfill({ status: 202, json: run()[0] });
    },
  );
  await page.goto("/organization/inspections?inspection=linked");
  await expect(
    page.getByRole("heading", { name: "Project Hail Mary" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Retry import", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Hardlink · keep seeding")).toBeVisible();
  await expect(page.getByText("1 book file · 3 extra files")).toBeVisible();
  await expect(page.getByLabel("Find catalog book")).not.toBeVisible();
  await expect(
    page.getByRole("button", { name: "Inspect files", exact: true }),
  ).not.toBeVisible();
  await expect(
    page.getByRole("button", { name: "Import resolved books", exact: true }),
  ).not.toBeVisible();
  await page.screenshot({
    path: "../../.local/import-review-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("button", { name: "Retry import", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../../.local/import-review-mobile.png",
    fullPage: true,
  });
  await page.getByText("Files & naming", { exact: true }).click();
  await expect(
    page.getByText("→ Project Hail Mary - Ray Porter.m4b"),
  ).toBeVisible();
  await page.getByRole("button", { name: "Retry import", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "View library book" }),
  ).toBeVisible();
  await expect(page.locator(".download-import-state")).toHaveText("In library");
  expect(retries).toBe(1);
  await page.reload();
  await expect(
    page.getByRole("link", { name: "View library book" }),
  ).toBeVisible();
  await expect(page.getByLabel("Find catalog book")).not.toBeVisible();
});
