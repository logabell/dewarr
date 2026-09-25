import { expect, test, type Page } from "../fixtures";

async function mockSettings(page: Page, role = "admin") {
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "admin",
          role,
          display_name: "Admin",
          onboarding_status: "complete",
          permissions: [],
        },
        csrf_token: "csrf-test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path.startsWith("/api/acquisition/preferences/"))
      data = {
        revision: "one",
        overrides: {},
        inherited: { ebook_library_id: "books", audio_library_id: "books" },
        inherited_origins: {
          ebook_library_id: "Installation default",
          audio_library_id: "Installation default",
        },
      };
    return route.fulfill({ json: data });
  });
}

const library = {
  id: "books",
  name: "Household books",
  accessible: true,
  integration_id: "abs",
  last_complete_sync: null,
};
const accounts = [
  {
    id: "admin",
    username: "admin",
    display_name: "Admin",
    role: "admin",
    active: true,
  },
  {
    id: "existing",
    username: "existing",
    display_name: "Existing member",
    role: "member",
    active: true,
  },
  {
    id: "reader",
    username: "plex-reader",
    display_name: "Plex reader",
    role: "member",
    active: true,
  },
  {
    id: "disabled",
    username: "disabled",
    display_name: "Disabled member",
    role: "member",
    active: false,
  },
  {
    id: "other",
    username: "other",
    display_name: "Other reader",
    role: "viewer",
    active: true,
  },
];

test("admin grants and revokes a member's library access while preserving other accounts", async ({
  page,
}, testInfo) => {
  await mockSettings(page);
  let grants = ["existing", "disabled"];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/auth/users", (route) =>
    route.fulfill({
      json: [
        ...accounts,
        ...["Alex", "Sam", "Morgan"].map((name) => ({
          id: name.toLowerCase(),
          username: name.toLowerCase(),
          display_name: name,
          role: "member",
          active: true,
        })),
      ],
    }),
  );
  await page.route(/\/api\/library\/libraries(?:\?.*)?$/, (route) =>
    route.fulfill({ json: [{ ...library, granted_user_ids: grants }] }),
  );
  await page.route("**/api/library/libraries/books/grants", (route) => {
    expect(route.request().method()).toBe("PUT");
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-test");
    const body = route.request().postDataJSON();
    expect(body.expected_user_ids).toEqual(grants);
    expect(body.user_ids).toEqual(
      expect.arrayContaining(["existing", "disabled"]),
    );
    grants = body.user_ids;
    return route.fulfill({ status: 204 });
  });
  await page.goto("/settings#libraries");
  await page
    .getByRole("button", { name: "Edit access to Household books" })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Library access: Household books",
  });
  await expect(
    dialog.getByRole("checkbox", { name: "Existing member" }),
  ).toBeChecked();
  await expect(
    dialog.getByRole("checkbox", { name: "Disabled member" }),
  ).toBeChecked();
  await expect(
    dialog.getByRole("checkbox", { name: "Admin", exact: true }),
  ).toHaveCount(0);
  const reader = dialog.getByRole("checkbox", { name: "Plex reader" });
  await expect(reader).not.toBeChecked();
  await dialog.getByRole("searchbox", { name: "Search people" }).fill("plex");
  await expect(dialog.getByRole("checkbox")).toHaveCount(1);
  await reader.check();
  await dialog.getByRole("searchbox", { name: "Search people" }).clear();
  await expect(
    dialog.getByRole("checkbox", { name: "Existing member" }),
  ).toBeChecked();
  await page.keyboard.press("Escape");
  await expect(
    dialog.getByText("Discard unsaved changes?", { exact: true }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Keep editing" }).click();
  await expect(reader).toBeChecked();
  await page.screenshot({
    path: testInfo.outputPath("library-access-desktop.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("library-access-mobile.png"),
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await dialog.getByRole("button", { name: "Save library access" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByText("Library access saved for Household books."),
  ).toBeVisible();
  expect(grants).toContain("reader");
  await page
    .getByRole("button", { name: "Edit access to Household books" })
    .click();
  await expect(reader).toBeChecked();
  await reader.uncheck();
  await dialog.getByRole("button", { name: "Save library access" }).click();
  await expect(dialog).toHaveCount(0);
  expect(grants).toEqual(["existing", "disabled"]);
  await page
    .getByRole("button", { name: "Edit access to Household books" })
    .click();
  await reader.check();
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await dialog.getByRole("button", { name: "Discard changes" }).click();
  await expect(dialog).toHaveCount(0);
  expect(grants).toEqual(["existing", "disabled"]);
  expect(errors).toEqual([]);
});

test("a stale library access edit reloads in place without overwriting another administrator", async ({
  page,
}) => {
  await mockSettings(page);
  let grants = ["existing"];
  let conflict = true;
  await page.route("**/api/auth/users", (route) =>
    route.fulfill({ json: accounts }),
  );
  await page.route(/\/api\/library\/libraries(?:\?.*)?$/, (route) =>
    route.fulfill({ json: [{ ...library, granted_user_ids: grants }] }),
  );
  await page.route("**/api/library/libraries/books/grants", (route) => {
    if (conflict) {
      conflict = false;
      grants = ["existing", "other"];
      return route.fulfill({
        status: 409,
        json: {
          detail:
            "Library access changed. Reload the saved settings to review it.",
        },
      });
    }
    const body = route.request().postDataJSON();
    expect(body.expected_user_ids).toEqual(grants);
    grants = body.user_ids;
    return route.fulfill({ status: 204 });
  });
  await page.goto("/settings#libraries");
  await page
    .getByRole("button", { name: "Edit access to Household books" })
    .click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("checkbox", { name: "Plex reader" }).check();
  await dialog.getByRole("button", { name: "Save library access" }).click();
  await expect(
    dialog.getByText("Library access changed.", { exact: false }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Reload saved settings" }).click();
  await expect(
    dialog.getByRole("checkbox", { name: "Other reader" }),
  ).toBeChecked();
  await dialog.getByRole("checkbox", { name: "Plex reader" }).check();
  await dialog.getByRole("button", { name: "Save library access" }).click();
  await expect(dialog).toHaveCount(0);
  expect(grants).toEqual(["existing", "other", "reader"]);
});

test("member sees actionable guidance for inaccessible defaults and it clears after a grant", async ({
  page,
}) => {
  await mockSettings(page, "member");
  let granted = false;
  let unavailable = false;
  const adminReads: string[] = [];
  await page.route("**/api/auth/users", (route) => {
    adminReads.push(route.request().url());
    return route.fulfill({ status: 403 });
  });
  await page.route(/\/api\/library\/libraries(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: unavailable
        ? [
            { ...library, accessible: false, granted_user_ids: [] },
            {
              ...library,
              id: "other-library",
              name: "Other books",
              granted_user_ids: [],
            },
          ]
        : granted
          ? [{ ...library, granted_user_ids: [] }]
          : [],
    }),
  );
  await page.goto("/settings#libraries");
  await expect(
    page.getByText("No libraries are available to your account.", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "Ebook library", exact: true }),
  ).toHaveValue("books");
  await expect(
    page
      .getByRole("combobox", { name: "Ebook library", exact: true })
      .locator("option:checked"),
  ).toHaveText("Unavailable saved library");
  await expect(
    page.getByRole("region", { name: "Library access", exact: true }),
  ).toHaveCount(0);
  granted = true;
  await page.reload();
  await expect(
    page
      .getByRole("combobox", { name: "Ebook library", exact: true })
      .locator("option:checked"),
  ).toHaveText("Household books");
  await expect(
    page.getByText("No libraries are available to your account.", {
      exact: false,
    }),
  ).toHaveCount(0);
  unavailable = true;
  await page.reload();
  await expect(
    page.getByText("A saved default library is unavailable to your account.", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("combobox", { name: "Ebook library", exact: true })
      .locator("option:checked"),
  ).toHaveText("Unavailable saved library");
  await page
    .getByRole("combobox", { name: "Ebook library", exact: true })
    .selectOption("other-library");
  await page
    .getByRole("combobox", { name: "Audiobook library", exact: true })
    .selectOption("other-library");
  await expect(
    page.getByText("A saved default library is unavailable to your account.", {
      exact: false,
    }),
  ).toHaveCount(0);
  expect(adminReads).toEqual([]);
});
