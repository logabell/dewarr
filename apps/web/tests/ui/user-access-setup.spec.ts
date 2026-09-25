import { expect, test } from "../fixtures";

const presets = [
  {
    id: "admin",
    label: "Administrator",
    permissions: ["admin", "manage_users", "request", "request_ebook"],
    description: "Manage the server.",
  },
  {
    id: "member",
    label: "Member",
    permissions: ["request", "request_ebook"],
    description: "Request books.",
  },
  {
    id: "viewer",
    label: "Viewer",
    permissions: [],
    description: "Browse shared books.",
  },
];
const custom = {
  id: "reading-role",
  name: "Reading group",
  permissions: ["request", "request_ebook"],
  description: "Request books for the group.",
};
const catalog = {
  presets,
  roles: [custom],
  permissions: [
    {
      name: "admin",
      label: "Administrator",
      group: "Administration",
      description: "Full access",
    },
    {
      name: "manage_users",
      label: "Manage users",
      group: "Administration",
      description: "Manage accounts",
    },
    {
      name: "request",
      label: "Request",
      group: "Requests",
      description: "Request books",
    },
    {
      name: "request_ebook",
      label: "Request ebooks",
      group: "Requests",
      description: "Request ebooks",
    },
  ],
};

test("add and edit a person with their role and libraries in one save", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  let users = [
    {
      id: "admin",
      username: "admin",
      display_name: "Admin",
      role: "admin",
      active: true,
      permissions: presets[0].permissions,
      permission_role_id: null as string | null,
      access_label: "Administrator",
      library_ids: [] as string[],
    },
  ];
  const libraries = [
    {
      id: "ebooks",
      name: "Ebooks",
      accessible: true,
      integration_id: "abs",
      granted_user_ids: [],
    },
    {
      id: "audio",
      name: "Audiobooks",
      accessible: true,
      integration_id: "abs",
      granted_user_ids: [],
    },
  ];
  const writes: { path: string; body: any }[] = [];
  let conflict = false;
  await page.route("**/api/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let data: unknown = [];
    if (["POST", "PUT"].includes(request.method()))
      writes.push({ path, body: request.postDataJSON() });
    if (path === "/api/auth/me")
      data = {
        user: { ...users[0], onboarding_status: "complete" },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/auth/access") data = catalog;
    else if (path === "/api/auth/oidc/settings")
      data = {
        enabled: false,
        button_label: "Sign in",
        allowed_groups: [],
        admin_groups: [],
        member_groups: [],
        viewer_groups: [],
      };
    else if (path === "/api/auth/plex/settings")
      data = { enabled: false, auto_register: false, default_role: "member" };
    else if (path === "/api/library/libraries") data = libraries;
    else if (path === "/api/auth/users") {
      if (request.method() === "POST") {
        const body = request.postDataJSON();
        expect(body.role_id).toBe(custom.id);
        expect(body.library_ids).toEqual(["ebooks", "audio"]);
        const created = {
          ...users[0],
          id: "reader",
          username: body.username,
          display_name: body.display_name,
          role: "member",
          permissions: custom.permissions,
          permission_role_id: custom.id,
          access_label: custom.name,
          library_ids: body.library_ids,
        };
        users = [...users, created];
        return route.fulfill({ status: 201, json: created });
      }
      data = users;
    } else if (path === "/api/auth/users/reader/permissions") {
      const body = request.postDataJSON();
      if (conflict) {
        conflict = false;
        users[1] = { ...users[1], library_ids: ["audio"] };
        return route.fulfill({
          status: 409,
          json: {
            detail:
              "Library access changed. Reload the saved settings to review it.",
          },
        });
      }
      expect(body.expected_library_ids).toEqual(users[1].library_ids);
      users[1] = {
        ...users[1],
        role: "viewer",
        permissions: body.permissions,
        permission_role_id: body.role_id,
        access_label: "Viewer",
        library_ids: body.library_ids,
      };
      data = users[1];
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#accounts");
  await page.getByRole("button", { name: "Add user", exact: true }).click();
  const add = page.getByRole("dialog", { name: "Add user" });
  await add.getByLabel("Name", { exact: true }).fill("Taylor Reader");
  await add.getByLabel("Username", { exact: true }).fill("taylor");
  await add
    .getByLabel("Password", { exact: true })
    .fill("a long test password");
  await add
    .getByRole("combobox", { name: "Role", exact: true })
    .selectOption(custom.id);
  await expect(
    add.getByText("No library access —", { exact: false }),
  ).toBeVisible();
  await add.getByRole("checkbox", { name: "Ebooks", exact: true }).check();
  await add.getByRole("checkbox", { name: "Audiobooks", exact: true }).check();
  await add.getByLabel("Password", { exact: true }).fill("");
  await page.screenshot({ path: testInfo.outputPath("add-user-desktop.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: testInfo.outputPath("add-user-mobile.png") });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await add
    .getByLabel("Password", { exact: true })
    .fill("a long test password");
  await add.getByRole("button", { name: "Add user", exact: true }).click();
  await expect(add).toHaveCount(0);
  expect(writes.map((write) => write.path)).toEqual(["/api/auth/users"]);
  const row = page.getByRole("row", { name: /Taylor Reader/ });
  await expect(row).toContainText("Reading group");
  await expect(row).toContainText("2 libraries");
  await row.getByRole("button", { name: "Edit Taylor Reader" }).click();
  const edit = page.getByRole("dialog", { name: "Edit Taylor Reader" });
  await expect(
    edit.getByRole("checkbox", { name: "Ebooks", exact: true }),
  ).toBeChecked();
  await edit
    .getByRole("combobox", { name: "Role", exact: true })
    .selectOption("preset:viewer");
  await edit.getByRole("checkbox", { name: "Ebooks", exact: true }).uncheck();
  conflict = true;
  await edit.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(
    edit.getByText("Library access changed.", { exact: false }),
  ).toBeVisible();
  await edit.getByRole("button", { name: "Reload saved settings" }).click();
  await expect(
    edit.getByRole("checkbox", { name: "Ebooks", exact: true }),
  ).not.toBeChecked();
  await expect(
    edit.getByRole("combobox", { name: "Role", exact: true }),
  ).toHaveValue(custom.id);
  await edit
    .getByRole("combobox", { name: "Role", exact: true })
    .selectOption("preset:viewer");
  await edit
    .getByRole("checkbox", { name: "Audiobooks", exact: true })
    .uncheck();
  await page.keyboard.press("Escape");
  await expect(
    edit.getByText("Discard unsaved changes?", { exact: true }),
  ).toBeVisible();
  await edit.getByRole("button", { name: "Keep editing" }).click();
  await edit.getByRole("button", { name: "Save changes", exact: true }).click();
  await expect(edit).toHaveCount(0);
  await expect(row).toContainText("Viewer");
  await expect(row).toContainText("No library access");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({
    path: testInfo.outputPath("users-access-desktop.png"),
  });
  expect(errors).toEqual([]);
});
