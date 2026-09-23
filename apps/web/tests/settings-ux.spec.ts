import { expect, test } from "./fixtures";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";

test("settings use compact controls and naming presets persist with planner previews", async ({
  page,
}, testInfo) => {
  test.setTimeout(90000);
  page.setDefaultTimeout(10000);
  execFileSync("uv", ["run", "python", "scripts/e2e_auth_budget.py"], {
    cwd: fileURLToPath(new URL("../../../", import.meta.url)),
    stdio: "pipe",
  });
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByLabel("Username", { exact: true })).toBeVisible();
  const bootstrap = await page.getByLabel("Your name").isVisible();
  await page.getByLabel("Username", { exact: true }).fill("reader");
  await page
    .getByLabel("Password", { exact: true })
    .fill("browser test password");
  if (bootstrap) {
    await page.getByLabel("Your name").fill("Test Reader");
    await page.getByRole("button", { name: "Create administrator" }).click();
    await expect(
      page.getByRole("heading", { name: "Let’s set up your library" }),
    ).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath("onboarding-desktop.png"),
      fullPage: true,
    });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
      path: testInfo.outputPath("onboarding-mobile.png"),
      fullPage: true,
    });
    await page
      .getByRole("button", { name: "Finish later", exact: true })
      .click();
  } else
    await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await page
    .getByRole("navigation", { name: "Settings categories" })
    .getByRole("link", { name: "Metadata", exact: true })
    .click();
  const catalog = page.getByRole("region", {
    name: "Metadata",
    exact: true,
  });
  await catalog.getByLabel("Primary language").selectOption("en");
  await expect(catalog.getByLabel("Primary language")).toHaveValue("en");
  await expect(
    catalog.getByLabel("Primary language").locator("option:checked"),
  ).toHaveText("English");
  const heights = await catalog
    .locator("select:visible,input:not([type=checkbox]):visible")
    .evaluateAll((nodes) =>
      nodes
        .map((node) => node.getBoundingClientRect().height)
        .filter((n) => n > 0),
    );
  expect(new Set(heights).size).toBe(1);
  await catalog
    .getByRole("button", { name: "About Hardcover", exact: true })
    .focus();
  await expect(catalog.getByRole("tooltip")).toContainText(
    "Minimum API permissions",
  );
  await expect(catalog.getByRole("tooltip")).toContainText("read:catalog");
  await expect(catalog.getByRole("tooltip")).toContainText("write:lists");
  await expect(
    catalog.getByRole("link", { name: "Create a token" }),
  ).toHaveAttribute(
    "href",
    /scope=read:catalog\+read:me:content\+read:lists\+read:library:public\+read:users\+write:lists/,
  );
  await page.keyboard.press("Escape");
  await expect(catalog.getByRole("tooltip")).toHaveCount(0);
  await page.screenshot({
    path: testInfo.outputPath("settings-desktop.png"),
    fullPage: false,
  });
  const jump = async (id: string) => {
    await page.goto(`/settings#${id}`);
  };
  await jump("naming");
  const naming = page.getByRole("region", { name: "File naming", exact: true });
  const preview = naming.getByLabel("Folder preview");
  await naming.getByRole("button", { name: "Audiobook", exact: true }).click();
  await expect(preview).toContainText("Harry Potter");
  await naming.getByRole("button", { name: "By series", exact: true }).click();
  await expect(
    naming.getByRole("checkbox", { name: "Narrator", exact: true }),
  ).not.toBeChecked();
  await naming.getByRole("checkbox", { name: "Narrator", exact: true }).click();
  await expect(preview).toContainText("Stephen Fry");
  await naming.getByRole("checkbox", { name: "Author", exact: true }).click();
  await expect(preview).not.toContainText("J. K. Rowling");
  await naming.getByRole("button", { name: "Ebook", exact: true }).click();
  await naming.getByRole("button", { name: "By author", exact: true }).click();
  await expect(
    naming.getByRole("checkbox", { name: "Narrator", exact: true }),
  ).toHaveCount(0);
  await expect(preview).toContainText("J. K. Rowling");
  await expect(preview).not.toContainText("Stephen Fry");
  const tokenOrder = naming.getByRole("list", { name: "Folder token order" });
  const titleHandle = tokenOrder.getByRole("button", {
    name: "Reorder {title} in Folder token order",
  });
  await titleHandle.focus();
  await page.keyboard.press("ArrowLeft");
  await expect(tokenOrder.locator(".naming-token-label")).toHaveText([
    "Title",
    "Author",
  ]);
  await titleHandle.dragTo(
    tokenOrder.locator("li").filter({ hasText: "Author" }),
  );
  await expect(tokenOrder.locator(".naming-token-label")).toHaveText([
    "Author",
    "Title",
  ]);
  await naming.getByRole("button", { name: "Save naming settings" }).click();
  await expect(naming.getByRole("status")).toHaveText("Saved");
  await page.reload();
  await naming.getByRole("button", { name: "Audiobook", exact: true }).click();
  await expect(
    naming.getByRole("checkbox", { name: "Author", exact: true }),
  ).not.toBeChecked();
  await expect(preview).toContainText("Stephen Fry");
  await naming
    .getByRole("checkbox", { name: "Publisher", exact: true })
    .check();
  await expect(preview).toContainText("Bloomsbury");
  await naming.getByRole("button", { name: "By series", exact: true }).click();
  await naming.getByRole("button", { name: "Save naming settings" }).click();
  await expect(naming.getByRole("status")).toHaveText("Saved");
  await naming.screenshot({ path: testInfo.outputPath("naming-desktop.png") });
  await jump("sources");
  const sources = page.getByRole("region", {
    name: "Download sources",
    exact: true,
  });
  await sources
    .getByLabel("mam_id", { exact: true })
    .waitFor({ state: "attached" });
  if (!(await sources.getByLabel("mam_id", { exact: true }).isVisible()))
    await sources.locator("summary").filter({ hasText: "MAM" }).click();
  await expect(sources.getByLabel("mam_id", { exact: true })).toBeVisible();
  await sources.screenshot({
    path: testInfo.outputPath("sources-desktop.png"),
  });
  await jump("preferences");
  const preferences = page.getByRole("region", {
    name: "Download preferences",
    exact: true,
  });
  await preferences.screenshot({
    path: testInfo.outputPath("preferences-desktop.png"),
  });
  await expect(preferences.getByLabel("Preferred narrators name")).toHaveCount(
    0,
  );
  await expect(
    preferences.getByLabel("Search first", { exact: true }),
  ).toHaveCount(0);
  await preferences
    .getByLabel("Default requested media", { exact: true })
    .selectOption("either");
  await expect(
    preferences.getByLabel("Search first", { exact: true }),
  ).toBeVisible();
  await expect(
    preferences.getByLabel("Ebook library", { exact: true }),
  ).toHaveCount(0);
  await jump("libraries");
  const libraries = page.getByRole("region", {
    name: "Libraries",
    exact: true,
  });
  await libraries
    .getByRole("button", { name: "Add server", exact: true })
    .click();
  await expect(
    libraries.getByRole("button", { name: "Audiobookshelf", exact: true }),
  ).toBeVisible();
  await expect(
    libraries.getByRole("button", { name: "Grimmory", exact: true }),
  ).toBeVisible();
  await libraries
    .getByRole("button", { name: "Audiobookshelf", exact: true })
    .click();
  const connectionForm = libraries.locator("form").filter({
    has: page.getByRole("heading", { name: "Connect your Audiobookshelf" }),
  });
  const saveConnection = connectionForm.getByRole("button", {
    name: "Save connection",
    exact: true,
  });
  await expect(saveConnection).toBeDisabled();
  await connectionForm
    .getByLabel("Server URL", { exact: true })
    .fill("http://127.0.0.1:13379/abs");
  await connectionForm
    .getByLabel("API token", { exact: true })
    .fill("incorrect-token");
  await expect(
    connectionForm.locator(".connection-result.failure"),
  ).toBeVisible();
  await expect(saveConnection).toBeDisabled();
  await connectionForm
    .getByLabel("API token", { exact: true })
    .fill("browser-abs-fixture-token");
  await expect(
    connectionForm.locator(".connection-result.success"),
  ).toContainText("1 libraries");
  await expect(saveConnection).toBeEnabled();
  await connectionForm
    .getByLabel("Server URL", { exact: true })
    .fill("http://127.0.0.1:13379/invalid-abs");
  await expect(saveConnection).toBeDisabled();
  await connectionForm
    .getByRole("button", { name: "Cancel", exact: true })
    .click();
  await jump("profiles");
  await expect(page).toHaveURL(/#preferences$/);
  await expect(
    page.getByRole("link", { name: "Saved profiles", exact: true }),
  ).toHaveCount(0);
  await jump("accounts");
  const accounts = page.getByRole("region", {
    name: "Users & access",
    exact: true,
  });
  await accounts.getByRole("button", { name: "Add user", exact: true }).click();
  const addUser = page.getByRole("dialog", { name: "Add user" });
  await addUser
    .getByLabel("Name", { exact: true })
    .fill("A reader with a deliberately long display name");
  await addUser.getByLabel("Username", { exact: true }).fill("layout-reader");
  await addUser.getByLabel("Password").fill("layout reader password");
  await addUser
    .getByRole("combobox", { name: "Role", exact: true })
    .selectOption("Member");
  await addUser.getByRole("button", { name: "Add user", exact: true }).click();
  await expect(
    accounts.getByRole("row", {
      name: /A reader with a deliberately long display name/,
    }),
  ).toContainText("Member");
  await page.setViewportSize({ width: 390, height: 844 });
  await jump("naming");
  await naming.screenshot({ path: testInfo.outputPath("naming-mobile.png") });
  await naming
    .getByRole("button", { name: "About naming preview", exact: true })
    .click();
  await expect(naming.getByRole("tooltip")).toBeVisible();
  expect(
    await naming
      .getByRole("tooltip")
      .evaluate((node) => node.getBoundingClientRect().right <= innerWidth),
  ).toBe(true);
  await page.keyboard.press("Escape");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await jump("catalog");
  await page.screenshot({
    path: testInfo.outputPath("settings-mobile.png"),
    fullPage: false,
  });
  expect(errors).toEqual([]);
});
