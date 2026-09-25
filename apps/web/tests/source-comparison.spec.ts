import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("release view sorting and filters compare every loaded result without changing acquisition policy", async ({
  page,
}, testInfo) => {
  test.setTimeout(60_000);
  execFileSync("uv", ["run", "python", "scripts/e2e_auth_budget.py"], {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    stdio: "pipe",
  });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await page.getByLabel("Username", { exact: true }).fill("reader");
  await page
    .getByLabel("Password", { exact: true })
    .fill("browser test password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Sign out", exact: true }),
  ).toBeVisible();
  await page.goto("/library?view=saved");
  await expect(page.getByRole("heading", { name: "My Library" })).toBeVisible();
  const works = await (
    await page.request.get("/api/catalog/works?q=The%20Synthetic%20Archive")
  ).json();
  const work = works.items.find(
    (item: { title: string }) => item.title === "The Synthetic Archive",
  );
  expect(work).toBeTruthy();
  const path = `/api/catalog/works/${work.id}/source-searches/latest`;
  let profile = "";
  let staleIdentity = false;
  // Expand a real completed fixture search only at the response boundary. This
  // validates the view, not tracker ranking or acquisition of these extra rows.
  await page.route(`**${path}*`, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    if (!body || body.status !== "completed" || !body.items.length) {
      await route.fulfill({ response });
      return;
    }
    profile = JSON.stringify(body.profile);
    const base = body.items[0];
    const items = Array.from({ length: 55 }, (_, index) => ({
      ...base,
      id: `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`,
      current_connection: index !== 16,
      assessment: {
        ...base.assessment,
        blocked: index === 15 ? ["Synthetic blocked format"] : [],
        review: [],
      },
      release: {
        ...base.release,
        source:
          index === 1
            ? "audiobookbay"
            : index === 2 || index === 3
              ? "prowlarr"
              : "mam",
        indexer_id: index === 2 ? "9" : index === 3 ? "10" : null,
        indexer_name: "Shared indexer name",
        title: `Release ${String(index).padStart(2, "0")}`,
        raw_title: `Original posting ${index}`,
        authors: ["Example Author"],
        narrators: [index === 54 ? "Final Narrator" : "Common Narrator"],
        formats: index === 0 ? [] : index === 2 ? ["PDF"] : ["m4b"],
        seeders: index === 0 ? null : index === 1 ? 0 : index === 54 ? 900 : 2,
        size_bytes:
          index === 0
            ? null
            : index === 1
              ? 0
              : index === 2 || index === 3
                ? 100
                : 1000 + index,
      },
    }));
    await route.fulfill({
      response,
      json: { ...body, items, stale_identity: staleIdentity },
    });
  });
  await page.goto(`/books/${work.id}?tab=sources`);
  const sources = page.getByRole("region", { name: "Book download sources" });
  const comparison = sources.getByRole("region", {
    name: "Compare loaded releases",
  });
  const cards = sources.locator("tr.source-release");
  await expect(
    sources.getByText("55 distinct releases · showing 1–50"),
  ).toBeVisible({ timeout: 30_000 });
  const originalProfile = profile;
  expect(originalProfile).not.toBe("");
  const savedProfiles = await (
    await page.request.get("/api/acquisition/profiles")
  ).json();
  const writes: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/api/") && request.method() !== "GET")
      writes.push(request.url());
  });
  await expect(cards).toHaveCount(50);
  await expect(cards.first()).toHaveAttribute("aria-label", "Release 00");
  await comparison.getByLabel("Sort this view").selectOption("seeds");
  await expect(cards.first()).toHaveAttribute("aria-label", "Release 54");
  await expect(cards.first()).toContainText("#55");
  await sources.locator(".infinite-scroll").scrollIntoViewIfNeeded();
  await expect(cards).toHaveCount(55);
  await expect(cards).toHaveCount(55);
  await expect(cards.nth(53).locator(".release-seeds")).toHaveText("0");
  await expect(cards.last().locator(".release-seeds")).toHaveText("Unknown");
  await comparison
    .getByLabel("Filter title, author or narrator")
    .fill("Final Narrator");
  await expect(cards).toHaveCount(1);
  await expect(cards.first()).toHaveAttribute("aria-label", "Release 54");
  await expect(
    sources.getByRole("button", { name: "Load more", exact: true }),
  ).toHaveCount(0);
  await comparison
    .getByLabel("Filter title, author or narrator")
    .fill("missing title");
  await expect(cards).toHaveCount(0);
  await expect(
    sources.getByText(/No loaded releases match these filters/),
  ).toBeVisible();
  await comparison.getByRole("button", { name: "Reset result view" }).click();
  await comparison.getByLabel("Sort this view").selectOption("smallest");
  await expect(cards.first()).toHaveAttribute("aria-label", "Release 01");
  await expect(cards.nth(1)).toHaveAttribute("aria-label", "Release 02");
  await expect(cards.nth(2)).toHaveAttribute("aria-label", "Release 03");
  await comparison.getByLabel("Sort this view").selectOption("largest");
  await expect(cards.first()).toHaveAttribute("aria-label", "Release 54");
  await sources.locator(".infinite-scroll").scrollIntoViewIfNeeded();
  await expect(cards).toHaveCount(55);
  await expect(cards.last()).toHaveAttribute("aria-label", "Release 00");
  await comparison.getByLabel("Result source").selectOption("prowlarr:10");
  await expect(cards).toHaveCount(1);
  await expect(cards.first()).toHaveAttribute("aria-label", "Release 03");
  await comparison.getByLabel("Result source").selectOption("");
  await comparison.getByLabel("Reported format").selectOption("pdf");
  await expect(cards).toHaveCount(1);
  await expect(cards.first()).toHaveAttribute("aria-label", "Release 02");
  await comparison.getByLabel("Reported format").selectOption("unknown");
  await expect(cards.first()).toContainText("Unknown");
  await comparison.getByLabel("Reported format").selectOption("");
  await comparison.getByLabel("Hide blocked or expired results").check();
  await expect(
    sources.getByText(/55 distinct releases · 53 match your filters/),
  ).toBeVisible();
  await comparison.getByLabel("Hide blocked or expired results").uncheck();
  await comparison.getByLabel("Sort this view").selectOption("title");
  await expect(cards.first()).toHaveAttribute("aria-label", "Release 00");
  for (const title of ["Release 15", "Release 16"]) {
    await sources
      .getByRole("button", { name: `Details for ${title}`, exact: true })
      .click();
    await expect(
      page
        .getByRole("dialog")
        .getByRole("button", { name: /^Download / }),
    ).toBeDisabled();
    await page.keyboard.press("Escape");
  }
  await comparison.screenshot({
    path: testInfo.outputPath("release-comparison-desktop.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await comparison.screenshot({
    path: testInfo.outputPath("release-comparison-mobile.png"),
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.reload();
  await expect(comparison.getByLabel("Sort this view")).toHaveValue("profile");
  await expect(
    comparison.getByLabel("Filter title, author or narrator"),
  ).toHaveValue("");
  expect(profile).toBe(originalProfile);
  expect(
    await (await page.request.get("/api/acquisition/profiles")).json(),
  ).toEqual(savedProfiles);
  staleIdentity = true;
  await page.reload();
  await expect(
    sources.getByText(/The catalog identity or series evidence changed/),
  ).toBeVisible();
  await cards
    .first()
    .getByRole("button", { name: /^Details for/ })
    .click();
  await expect(
    page.getByRole("dialog").getByRole("button", { name: /^Download / }),
  ).toBeDisabled();
  expect(writes).toEqual([]);
  expect(errors).toEqual([]);
});
