import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

test("restored connections review credentials and downloader paths while keeping work paused", async ({
  page,
}) => {
  test.setTimeout(180_000);
  page.setDefaultTimeout(15_000);
  const root = fileURLToPath(new URL("../../../", import.meta.url));
  const fixture = (mode: string) =>
    execFileSync("uv", ["run", "python", "scripts/e2e_recovery.py", mode], {
      cwd: root,
      stdio: "pipe",
    });
  const evidence = root + "/.local/evidence/recovery-connections-ui";
  mkdirSync(evidence, { recursive: true });
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
  const backends = await (await page.request.get("/api/integrations")).json();
  const downloaders = await (await page.request.get("/api/downloaders")).json();
  const backend = backends.find(
    (item: { base_url: string }) =>
      item.base_url === "http://127.0.0.1:13379/abs",
  );
  const downloader = downloaders.find(
    (item: { name: string }) => item.name === "Fixture qBittorrent",
  );
  expect(backend).toBeTruthy();
  expect(downloader).toBeTruthy();
  // A running app would react to the pause with its own navigation and abort the next goto.
  await page.goto("about:blank");
  fixture("pause");
  try {
    await page.goto("/");
    await page.getByLabel("Username", { exact: true }).fill("reader");
    await page
      .getByLabel("Password", { exact: true })
      .fill("browser test password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "Your restored library is paused" }),
    ).toBeVisible();
    const before = await (
      await page.request.get("http://127.0.0.1:13379/fixture/recovery-stats")
    ).json();
    async function observe() {
      const previous = (await (await page.request.get("/api/recovery")).json())
        .latest_scan?.id;
      await page
        .getByRole("button", { name: "Run read-only checks", exact: true })
        .click();
      await expect
        .poll(
          async () =>
            (await (await page.request.get("/api/recovery")).json()).latest_scan
              ?.id,
        )
        .not.toBe(previous);
      await expect(
        page.getByRole("status").filter({ hasText: "Observation finished" }),
      ).toBeVisible({ timeout: 60_000 });
      await page.getByLabel("Filter observations").selectOption("review");
    }
    await observe();
    await page
      .getByRole("button", {
        name: `Review settings for Connection · ${downloader.name}`,
        exact: true,
      })
      .click();
    const editor = page.getByRole("group", {
      name: `Settings for ${downloader.name}`,
      exact: true,
    });
    const savePath = downloader.save_path.replace(/\/$/, "") + "/reviewed";
    await editor
      .getByRole("textbox", { name: "Download save path", exact: true })
      .fill(savePath);
    await editor
      .getByRole("textbox", { name: "Download category", exact: true })
      .fill("reviewed-books");
    await editor
      .getByRole("textbox", { name: "New downloader username", exact: true })
      .fill("browser-qbit-user");
    await editor
      .getByLabel("New downloader password", { exact: true })
      .fill("browser-qbit-password");
    await expect(
      editor.getByRole("combobox", {
        name: "Worker download root",
        exact: true,
      }),
    ).toHaveValue(downloader.mappings[0].source_key);
    // Capture routing controls without retaining entered credentials in screenshots.
    await editor
      .getByRole("group", { name: "Download path mappings", exact: true })
      .screenshot({ path: evidence + "/paths.png" });
    await editor
      .getByRole("button", { name: "Preview connection settings", exact: true })
      .click();
    const preview = page.locator(
      'section[aria-labelledby="connection-review-title"]',
    );
    await expect(
      preview.getByRole("heading", {
        name: "Review connection settings",
        exact: true,
      }),
    ).toBeFocused();
    await expect(preview).toContainText(downloader.save_path);
    await expect(preview).toContainText(savePath);
    await expect(preview).toContainText("reviewed-books");
    await expect(preview).toContainText(
      "replace with the newly entered credentials",
    );
    await expect(preview).not.toContainText("browser-qbit-password");
    await preview.screenshot({ path: evidence + "/preview.png" });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await preview.screenshot({ path: evidence + "/paths-mobile.png" });
    await page.setViewportSize({ width: 1440, height: 1000 });

    await preview
      .getByRole("button", {
        name: "Confirm reviewed connection settings",
        exact: true,
      })
      .click();
    await expect(preview.getByRole("status")).toContainText(
      "Connection settings reviewed",
      { timeout: 30_000 },
    );
    const qbitReview = (await (await page.request.get("/api/recovery")).json())
      .latest_connection_reconciliation;
    expect(qbitReview.status).toBe("completed");
    expect(qbitReview.results[0]).toMatchObject({
      integration_id: downloader.id,
      state: "tested",
      changed: true,
    });
    expect(JSON.stringify(qbitReview)).not.toContain("browser-qbit-password");
    expect(JSON.stringify(qbitReview)).not.toContain("encrypted_secrets");
    expect((await page.request.get("/api/downloaders")).status()).toBe(423);
    expect(
      await (
        await page.request.get("http://127.0.0.1:13379/fixture/recovery-stats")
      ).json(),
    ).toEqual(before);
    await observe();
    await page
      .getByRole("button", {
        name: `Review settings for Connection · ${backend.name}`,
        exact: true,
      })
      .click();
    const absEditor = page.getByRole("group", {
      name: `Settings for ${backend.name}`,
      exact: true,
    });
    await absEditor
      .getByLabel("New API token", { exact: true })
      .fill("browser-abs-fixture-token");
    await absEditor
      .getByRole("button", { name: "Preview connection settings", exact: true })
      .click();
    await expect(
      preview.getByRole("heading", {
        name: "Review connection settings",
        exact: true,
      }),
    ).toBeFocused();
    await expect(preview).not.toContainText("browser-abs-fixture-token");
    await preview
      .getByRole("button", {
        name: "Confirm reviewed connection settings",
        exact: true,
      })
      .click();
    await expect(preview.getByRole("status")).toContainText(
      "Connection settings reviewed",
      { timeout: 30_000 },
    );
    const absReview = (await (await page.request.get("/api/recovery")).json())
      .latest_connection_reconciliation;
    expect(absReview.results[0]).toMatchObject({
      integration_id: backend.id,
      state: "tested",
      changed: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.reload();
    await expect(preview.getByRole("status")).toContainText(
      "Connection settings reviewed",
    );
    await preview.screenshot({ path: evidence + "/mobile.png" });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    expect((await page.request.get("/api/lists")).status()).toBe(423);
    expect(
      (await (await page.request.get("/api/recovery")).json()).resume_available,
    ).toBe(false);
    expect(
      await (
        await page.request.get("http://127.0.0.1:13379/fixture/recovery-stats")
      ).json(),
    ).toEqual(before);
    expect(errors).toEqual([]);
  } finally {
    fixture("clear");
  }
});
