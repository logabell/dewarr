import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("download defaults inherit per field and persist after reload", async ({
  page,
}, testInfo) => {
  execFileSync("uv", ["run", "python", "scripts/e2e_auth_budget.py"], {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    stdio: "pipe",
  });
  await page.goto("/");
  await page.getByLabel("Username", { exact: true }).fill("reader");
  await page
    .getByLabel("Password", { exact: true })
    .fill("browser test password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Sign out", exact: true }),
  ).toBeVisible();
  await page.goto("/download-preferences");
  const panel = page.getByRole("region", {
    name: "Download defaults",
    exact: true,
  });
  await panel.getByText("Downloader defaults", { exact: true }).click();
  const torrentDefault = panel.getByLabel("Default torrent downloader", {
    exact: true,
  });
  const usenetDefault = panel.getByLabel("Default Usenet downloader", {
    exact: true,
  });
  await expect(torrentDefault).toBeVisible();
  await expect(usenetDefault).toBeVisible();
  const options = await (
    await page.request.get("/api/acquisition/selections/options")
  ).json();
  const torrents = options.downloaders.filter(
    (item: { protocol: string }) => item.protocol === "torrent",
  );
  expect(torrents).toHaveLength(1);
  await expect(torrentDefault).toHaveValue(torrents[0].id);
  await expect(torrentDefault).toBeDisabled();
  await expect(usenetDefault).toHaveValue("");
  await panel.getByLabel("Apply to").selectOption("installation");
  await panel.getByText("Formats and transfer limits", { exact: true }).click();
  await panel
    .getByRole("button", {
      name: "Reorder pdf in Ebook format preference",
      exact: true,
    })
    .press("ArrowUp");
  await panel
    .getByRole("button", {
      name: "Reorder pdf in Ebook format preference",
      exact: true,
    })
    .press("ArrowUp");
  await panel
    .getByRole("button", {
      name: "Reorder pdf in Ebook format preference",
      exact: true,
    })
    .press("ArrowUp");
  await panel
    .getByRole("button", { name: "Save download defaults", exact: true })
    .click();
  await expect(panel.getByRole("status")).toContainText(
    "Download defaults saved",
  );
  await panel.getByLabel("Apply to").selectOption("personal");
  await panel.getByText("Formats and transfer limits", { exact: true }).click();
  const ebooks = panel.getByRole("group", {
    name: "Ebook format preference",
    exact: true,
  });
  await expect(ebooks.getByRole("listitem").first()).toContainText("PDF");
  await panel
    .getByRole("button", {
      name: "Reorder epub in Ebook format preference",
      exact: true,
    })
    .press("ArrowUp");
  await panel
    .getByRole("button", { name: "Save download defaults", exact: true })
    .click();
  await expect(panel.getByRole("status")).toContainText(
    "Download defaults saved",
  );
  await page.reload();
  await panel.getByText("Formats and transfer limits", { exact: true }).click();
  await expect(ebooks.getByRole("listitem").first()).toContainText("EPUB");
  await panel
    .getByRole("button", {
      name: "Use inherited Ebook format preference",
      exact: true,
    })
    .click();
  await expect(ebooks.getByRole("listitem").first()).toContainText("PDF");
  await panel
    .getByRole("button", { name: "Save download defaults", exact: true })
    .click();
  await expect(panel.getByRole("status")).toContainText(
    "Download defaults saved",
  );
  await expect(panel.getByRole("status")).toContainText(
    "Download defaults saved",
  );
  await page.reload();
  const profiles = await (
    await page.request.get("/api/acquisition/profiles")
  ).json();
  expect(profiles[0].preferences.ebook_formats[0]).toBe("pdf");
  expect(profiles[0].origins.ebook_formats).toBe("Installation default");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("download-defaults-mobile.png"),
    fullPage: true,
  });
  await panel.getByLabel("Apply to").selectOption("installation");
  await panel
    .getByRole("button", { name: "Use inherited defaults", exact: true })
    .click();
  await panel
    .getByRole("button", { name: "Save download defaults", exact: true })
    .click();
  await expect(panel.getByRole("status")).toContainText(
    "Download defaults saved",
  );
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
});
