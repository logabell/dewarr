import { expect, test } from "./fixtures";

test("recovery settings preserve source overrides and manage the release blocklist", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("Username", { exact: true }).fill("reader");
  await page
    .getByLabel("Password", { exact: true })
    .fill("browser test password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Sign out", exact: true }),
  ).toBeVisible();
  await page.goto("/settings#recovery");
  const form = page.getByRole("form", { name: "Download recovery settings" });
  await expect(
    form.getByLabel("Maximum attempts", { exact: false }),
  ).toHaveValue("3");
  const mam = form.getByRole("group", { name: "MyAnonamouse", exact: true });
  await expect(
    mam.getByLabel("No progress or zero seeders", { exact: false }),
  ).toHaveValue("");
  await mam
    .getByLabel("No progress or zero seeders", { exact: false })
    .fill("168");
  await form.getByLabel("Maximum attempts", { exact: false }).fill("4");
  await form.getByRole("button", { name: "Save recovery settings" }).click();
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get("/api/acquisition/recovery/settings")
          ).json()
        ).attempt_cap,
    )
    .toBe(4);
  await page.reload();
  await expect(
    mam.getByLabel("No progress or zero seeders", { exact: false }),
  ).toHaveValue("168");
  await expect(
    form.getByLabel("Maximum attempts", { exact: false }),
  ).toHaveValue("4");
  const block = {
    id: "12345678-1234-4234-8234-123456789abc",
    work_id: "12345678-1234-4234-8234-123456789abd",
    medium: "audio",
    source: "mam",
    title: "A stalled release",
    reason: "No seeders for 24 hours",
    actor_id: "12345678-1234-4234-8234-123456789abe",
    automatic: true,
    work_title: "Recovery book",
    actor_name: "Test Reader",
    created_at: new Date().toISOString(),
  };
  let removed = false;
  await page.route("**/api/acquisition/recovery/blocklist**", async (route) => {
    if (route.request().method() === "DELETE") {
      removed = true;
      await route.fulfill({ status: 204 });
    } else await route.fulfill({ json: removed ? [] : [block] });
  });
  await page.reload();
  await page.getByText("Release blocklist", { exact: true }).click();
  await expect(
    page.getByRole("list", { name: "Blocked releases" }),
  ).toContainText("No seeders for 24 hours");
  await page.getByRole("button", { name: "Remove from blocklist" }).click();
  await expect(
    page.getByText("No blocked releases on this page."),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  // Restore defaults so other browser tests retain their starting policy.
  await mam
    .getByLabel("No progress or zero seeders", { exact: false })
    .fill("");
  await form.getByLabel("Maximum attempts", { exact: false }).fill("3");
  await form.getByRole("button", { name: "Save recovery settings" }).click();
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get("/api/acquisition/recovery/settings")
          ).json()
        ).attempt_cap,
    )
    .toBe(3);

  const auth = await (await page.request.get("/api/auth/me")).json();
  const headers = {
    "X-CSRF-Token": auth.csrf_token,
    Origin: "http://127.0.0.1:8001",
    "Idempotency-Key": crypto.randomUUID(),
  };
  const workResponse = await page.request.post("/api/catalog/works", {
    headers,
    data: { title: "Recovery activity book", authors: [] },
  });
  expect(workResponse.ok()).toBe(true);
  const work = await workResponse.json();
  const requestResponse = await page.request.post("/api/requests", {
    headers,
    data: { work_id: work.id, specification: { mode: "audio" } },
  });
  expect(requestResponse.ok()).toBe(true);
  const saved = (await requestResponse.json()).request;
  const attemptId = "22345678-1234-4234-8234-123456789abc";
  const selectionId = "32345678-1234-4234-8234-123456789abc";
  await page.route("**/api/requests?*", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            ...saved,
            targets: saved.targets.map((target: object) => ({
              ...target,
              attempt_id: attemptId,
              attempt_state: "complete",
              can_view_download_history: true,
            })),
          },
        ],
        total: 1,
        offset: 0,
        limit: 10,
      },
    }),
  );
  let reported = false;
  await page.route(`**/api/acquisition/downloads/${attemptId}?*`, (route) =>
    route.fulfill({
      json: {
        id: attemptId,
        selection_id: selectionId,
        can_report_problem: !reported,
        attempt_chain: [
          {
            attempt_id: attemptId,
            selection_id: selectionId,
            release_title: "First release",
            state: reported ? "held" : "complete",
            reason: reported ? "Reported problem: bad-audio" : "Downloaded",
          },
        ],
        recoveries: reported
          ? [
              {
                id: attemptId,
                message: "Waiting for administrator approval",
                cleanup: "pending",
                can_approve: false,
              },
            ]
          : [],
      },
    }),
  );
  await page.route("**/api/acquisition/recovery/reports", async (route) => {
    expect(route.request().postDataJSON()).toMatchObject({
      selection_id: selectionId,
      reason: "bad-audio",
      require_approval: true,
    });
    reported = true;
    await route.fulfill({ status: 202, json: [] });
  });
  await page.goto("/requests");
  await page
    .getByText("Download history and recovery", { exact: true })
    .click();
  await expect(
    page.getByRole("list", { name: "Download attempt chain" }),
  ).toContainText("First release");
  await page
    .getByRole("button", { name: "Report a problem", exact: true })
    .click();
  await page.getByLabel("Problem with this release").selectOption("bad-audio");
  await page
    .getByLabel("Wait for administrator approval before replacement")
    .check();
  await page
    .getByRole("button", { name: "Report and request replacement" })
    .click();
  await expect(
    page.getByText("Waiting for administrator approval", { exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
