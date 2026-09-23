import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

test("restored sources save explicit settings and verify rotating sessions without resuming", async ({
  page,
}) => {
  test.setTimeout(300_000);
  page.setDefaultTimeout(15_000);
  const root = fileURLToPath(new URL("../../../", import.meta.url));
  const fixture = (mode: string) =>
    execFileSync("uv", ["run", "python", "scripts/e2e_recovery.py", mode], {
      cwd: root,
      stdio: "pipe",
    });
  const evidence = root + "/.local/evidence/recovery-sources-ui";
  mkdirSync(evidence, { recursive: true });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  async function login() {
    await page.goto("/");
    await page.getByLabel("Username", { exact: true }).fill("reader");
    await page
      .getByLabel("Password", { exact: true })
      .fill("browser test password");
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "Sign out", exact: true }),
    ).toBeVisible();
  }
  await login();
  const identity = await (await page.request.get("/api/auth/me")).json();
  const headers = {
    "X-CSRF-Token": identity.csrf_token,
    Origin: "http://127.0.0.1:8001",
  };
  for (const [key, base_url, extra] of [
    [
      "prowlarr",
      "http://127.0.0.1:13379/prowlarr",
      { api_key: "browser-prowlarr-key" },
    ],
    ["audiobookbay", "https://abb.test", { enabled: false }],
  ] as const) {
    const before = await (
      await page.request.get(`/api/sources/${key}/connection`)
    ).json();
    const configured = await page.request.put(
      `/api/sources/${key}/connection`,
      {
        headers,
        data: { base_url, expected_generation: before.generation, ...extra },
      },
    );
    expect(configured.status()).toBe(200);
  }
  // Leave the app before its restored-state redirect can race the next login navigation.
  await page.goto("about:blank");
  fixture("pause");
  try {
    await login();
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
    async function edit(name: string) {
      await page
        .getByRole("button", {
          name: `Review settings for Source · ${name}`,
          exact: true,
        })
        .click();
      return page.getByRole("group", {
        name: `Settings for ${name}`,
        exact: true,
      });
    }
    const preview = page.locator(
      'section[aria-labelledby="source-review-title"]',
    );
    async function confirm() {
      await expect(
        preview.getByRole("heading", {
          name: "Review source settings",
          exact: true,
        }),
      ).toBeFocused();
      await preview
        .getByRole("button", {
          name: "Confirm reviewed source settings",
          exact: true,
        })
        .click();
    }
    async function verified(status: string) {
      await expect
        .poll(
          async () =>
            (await (await page.request.get("/api/recovery")).json())
              .latest_source_reconciliation?.verification?.status,
          { timeout: 40_000 },
        )
        .toBe(status);
      await expect(preview).toContainText(
        `Connection verification · ${status}`,
      );
    }
    await observe();
    let editor = await edit("MyAnonamouse");
    await expect(editor).toContainText("Enter a current mam_id");
    await editor
      .getByLabel("Current mam_id", { exact: true })
      .fill("outdated-browser-cookie");
    await editor
      .getByRole("button", { name: "Preview source settings", exact: true })
      .click();
    await expect(preview).not.toContainText("outdated-browser-cookie");
    await confirm();
    await verified("held");
    await expect(preview).toContainText("Settings remain saved");
    await preview.screenshot({ path: evidence + "/failed-verification.png" });
    await observe();
    editor = await edit("MyAnonamouse");
    const token = (
      await (
        await page.request.get("http://127.0.0.1:13379/fixture/mam-session")
      ).json()
    ).cookie;
    await editor.getByLabel("Current mam_id", { exact: true }).fill(token);
    await editor
      .getByRole("button", { name: "Preview source settings", exact: true })
      .click();
    await expect(
      preview.getByRole("heading", {
        name: "Review source settings",
        exact: true,
      }),
    ).toBeFocused();
    await expect(preview).not.toContainText(token);
    await preview.screenshot({ path: evidence + "/mam-preview.png" });
    await confirm();
    await verified("completed");
    await page.reload();
    await expect(preview).toContainText("Source connection verified");
    const state = (await (await page.request.get("/api/recovery")).json())
      .latest_source_reconciliation;
    expect(state.status).toBe("completed");
    expect(JSON.stringify(state)).not.toContain(token);
    expect(JSON.stringify(state)).not.toContain("encrypted_secrets");
    expect(
      (await page.request.get("/api/sources/mam/connection")).status(),
    ).toBe(423);
    await observe();
    editor = await edit("Prowlarr");
    await editor
      .getByLabel("Excluded indexer IDs", { exact: true })
      .fill("7, 12");
    await editor
      .getByLabel("New API key", { exact: true })
      .fill("browser-prowlarr-key");
    await editor
      .getByRole("button", { name: "Preview source settings", exact: true })
      .click();
    await expect(preview).toContainText("Excluded indexers: 7, 12");
    await expect(preview).not.toContainText("browser-prowlarr-key");
    await confirm();
    await verified("completed");
    await observe();
    editor = await edit("AudiobookBay");
    await editor
      .getByLabel("Proxy URL", { exact: true })
      .fill("http://gluetun.test:8888");
    await expect(
      editor.getByRole("checkbox", { name: "Enable source", exact: true }),
    ).not.toBeChecked();
    await editor
      .getByRole("button", { name: "Preview source settings", exact: true })
      .click();
    await expect(preview).toContainText(
      "Required proxy · http://gluetun.test:8888",
    );
    await confirm();
    await expect
      .poll(
        async () =>
          (await (await page.request.get("/api/recovery")).json())
            .latest_source_reconciliation?.status,
      )
      .toBe("completed");
    expect(
      (await (await page.request.get("/api/recovery")).json())
        .latest_source_reconciliation.verification,
    ).toBeNull();
    await expect(preview).toContainText("Source settings saved.");
    await page.setViewportSize({ width: 390, height: 844 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await preview.screenshot({ path: evidence + "/source-mobile.png" });
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
