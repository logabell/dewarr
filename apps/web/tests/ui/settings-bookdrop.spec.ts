import { expect, test } from "../fixtures";

test("switching from a personal Bookdrop default reuses the existing direct route revision", async ({
  page,
}) => {
  const bookdrop = {
    id: "intake",
    workflow: "bookdrop",
    integration_id: "server",
    library_id: null,
    medium: "ebook",
    root_key: "bookdrop-server",
    backend_path: "/bookdrop",
    local_path: "/bookdrop",
    mode: "copy",
    revision: "intake-revision",
    server_kind: "grimmory",
    publication_available: false,
  };
  let direct: any = {
    id: "direct",
    workflow: "library",
    library_id: "old-library",
    medium: "ebook",
    root_key: "library-ebook",
    backend_path: "/old-books",
    local_path: "/old-books",
    mode: "copy",
    revision: "direct-revision",
    server_kind: "grimmory",
    publication_available: false,
  };
  let submitted: any;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "admin",
          role: "admin",
          display_name: "Reader",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/organization/destinations")
      data = [direct, bookdrop];
    else if (path.startsWith("/api/acquisition/preferences/"))
      data = {
        effective: {
          ebook_destination_id: path.endsWith("/personal")
            ? "intake"
            : "direct",
        },
        inherited: {},
        overrides: {},
        revision: "one",
      };
    else if (path.endsWith("/automatic-import"))
      data = { requested_enabled: false, enabled: false, generation: 1 };
    else if (path === "/api/organization/library-folders")
      data = [
        {
          library_id: null,
          integration_id: "server",
          workflow: "bookdrop",
          library_name: "Bookdrop review",
          server_name: "Grimmory",
          server_kind: "grimmory",
          folders: [""],
          ebooks_allowed: true,
        },
        {
          library_id: "new-library",
          workflow: "library",
          library_name: "New library",
          server_name: "Grimmory",
          server_kind: "grimmory",
          folders: ["/new-books"],
          ebooks_allowed: true,
        },
      ];
    else if (path === "/api/organization/library-folders/ebook") {
      submitted = route.request().postDataJSON();
      if (
        submitted.destination_id !== "direct" ||
        submitted.expected_revision !== "direct-revision"
      ) {
        await route.fulfill({
          status: 409,
          json: {
            detail:
              "Folder settings changed. Reopen the folder picker and try again.",
          },
        });
        return;
      }
      direct = { ...direct, ...submitted, revision: "updated" };
      data = direct;
    }
    await route.fulfill({ json: data });
  });
  await page.goto("/settings#libraries");
  await expect(
    page.getByText("Bookdrop review · Grimmory", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Change ebooks folder" }).click();
  const dialog = page.getByRole("dialog", { name: "Choose ebooks folder" });
  await dialog
    .getByRole("combobox", { name: "Library", exact: true })
    .selectOption("new-library|/new-books");
  await dialog.locator('button[type="submit"]').click();
  await expect(dialog).not.toBeVisible();
  expect(submitted).toMatchObject({
    library_id: "new-library",
    workflow: "library",
    destination_id: "direct",
    expected_revision: "direct-revision",
  });
});

test("Bookdrop setup keeps review separate from direct import and saves copy-only intake", async ({
  page,
}) => {
  let saved: any = null;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "admin",
          role: "admin",
          display_name: "Reader",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/organization/library-folders")
      data = [
        {
          library_id: "library",
          workflow: "library",
          library_name: "Ebooks",
          server_name: "My Grimmory",
          server_kind: "grimmory",
          folders: ["/books"],
          ebooks_allowed: true,
          audio_allowed: false,
        },
        {
          library_id: null,
          integration_id: "server",
          workflow: "bookdrop",
          library_name: "Bookdrop review",
          server_name: "My Grimmory",
          server_kind: "grimmory",
          folders: [""],
          ebooks_allowed: true,
          audio_allowed: false,
        },
      ];
    else if (path === "/api/organization/destinations")
      data = saved ? [saved] : [];
    else if (path.startsWith("/api/acquisition/preferences/"))
      data = { effective: {}, inherited: {}, overrides: {}, revision: "one" };
    else if (path === "/api/organization/library-folders/ebook") {
      saved = {
        ...route.request().postDataJSON(),
        id: "destination",
        medium: "ebook",
        root_key: "bookdrop-server",
        server_kind: "grimmory",
        revision: "saved",
        publication_available: false,
      };
      data = saved;
    }
    await route.fulfill({ json: data });
  });
  await page.goto("/settings#libraries");
  await page.getByRole("button", { name: "Choose ebooks folder" }).click();
  const dialog = page.getByRole("dialog", { name: "Choose ebooks folder" });
  await expect(
    dialog.getByText(/global Move files to library pattern must be off/),
  ).toBeVisible();
  await dialog
    .getByRole("combobox", { name: "Library", exact: true })
    .selectOption("server|bookdrop");
  await dialog
    .getByRole("textbox", { name: "Bookdrop folder in Grimmory" })
    .fill("/bookdrop");
  await expect(
    dialog.getByText(/Grimmory chooses the final name and library/),
  ).toBeVisible();
  await expect(
    dialog.getByRole("checkbox", { name: /Send to Bookdrop on completion/ }),
  ).toBeChecked();
  const submit = dialog.locator('button[type="submit"]');
  await submit.click();
  await expect(dialog).not.toBeVisible();
  expect(saved).toMatchObject({
    workflow: "bookdrop",
    integration_id: "server",
    library_id: null,
    backend_path: "/bookdrop",
    local_path: "/bookdrop",
    mode: "copy",
    seeding_rename: false,
  });
  await expect(
    page.getByText("Bookdrop review · Grimmory", { exact: true }),
  ).toBeVisible();
});
