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

test("account removal and provider unlink require reviewed username confirmation", async ({
  page,
}) => {
  const admin = {
    id: "admin",
    username: "admin",
    display_name: "Admin",
    role: "admin",
    active: true,
    permissions: presets[0].permissions,
    access_label: "Administrator",
    library_ids: [],
    onboarding_status: "complete",
  };
  const reader = {
    ...admin,
    id: "reader",
    username: "reader",
    display_name: "Reader",
    role: "member",
    permissions: presets[1].permissions,
    access_label: "Member",
  };
  let users = [admin, reader];
  let methods = {
    local_password: true,
    oidc: true,
    plex: false,
    revision: "first",
  };
  const writes: string[] = [];
  let methodReads = 0;
  let stale = true;
  let releaseDelete!: () => void;
  const deletion = new Promise<void>((resolve) => {
    releaseDelete = resolve;
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me") data = { user: admin, csrf_token: "test" };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/auth/access") data = catalog;
    else if (path === "/api/auth/users") data = users;
    else if (path === "/api/auth/users/reader/sign-in") {
      methodReads++;
      data = methods;
    } else if (request.method() === "DELETE") {
      writes.push(path);
      const body = request.postDataJSON();
      expect(body.confirm_username).toBe(stale ? "reader" : "reader-renamed");
      expect(body.expected_revision).toBe(stale ? "first" : methods.revision);
      if (path.endsWith("/providers/oidc")) {
        if (stale) {
          stale = false;
          users = [admin, { ...reader, username: "reader-renamed" }];
          methods = { ...methods, revision: "renamed" };
          return route.fulfill({
            status: 409,
            json: { detail: "This account changed; reload it" },
          });
        }
        methods = { ...methods, oidc: false, revision: "unlinked" };
        data = methods;
      } else {
        expect(path).toBe("/api/auth/users/reader");
        await deletion;
        users = [admin];
        return route.fulfill({ status: 204 });
      }
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#accounts");
  await page
    .getByRole("button", { name: "Account details for Reader", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "Account details for Reader",
    exact: true,
  });
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("Reader updated");
  await expect(
    dialog.getByText(
      "Save account changes before unlinking a provider or deleting the account.",
    ),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Unlink OIDC", exact: true }),
  ).toBeDisabled();
  await dialog.getByLabel("Display name", { exact: true }).fill("Reader");
  await dialog
    .getByRole("button", { name: "Unlink OIDC", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: "Unlink provider", exact: true }),
  ).toBeDisabled();
  await dialog.getByLabel("Type reader to confirm").fill("wrong");
  await expect(
    dialog.getByRole("button", { name: "Unlink provider", exact: true }),
  ).toBeDisabled();
  expect(writes).toEqual([]);
  await dialog.getByLabel("Type reader to confirm").fill("reader");
  // A background refresh must not silently replace the reviewed identity.
  methods = { ...methods, revision: "background-change" };
  const readsBeforeRefresh = methodReads;
  await page.clock.install();
  await page.clock.fastForward(31_000);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("offline"));
    window.dispatchEvent(new Event("online"));
  });
  await expect.poll(() => methodReads).toBeGreaterThan(readsBeforeRefresh);
  await dialog
    .getByRole("button", { name: "Unlink provider", exact: true })
    .click();
  await expect(
    dialog.getByText("This account changed; reload it"),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "Reload sign-in details" }).click();
  await dialog
    .getByRole("button", { name: "Unlink OIDC", exact: true })
    .click();
  await expect(dialog.getByLabel("Type reader-renamed to confirm")).toHaveValue(
    "",
  );
  await expect(
    dialog.getByRole("button", { name: "Unlink provider", exact: true }),
  ).toBeDisabled();
  await dialog
    .getByLabel("Type reader-renamed to confirm")
    .fill("reader-renamed");
  await dialog
    .getByRole("button", { name: "Unlink provider", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: "Unlink OIDC", exact: true }),
  ).toHaveCount(0);
  await dialog.getByRole("button", { name: "Delete unused account" }).click();
  await expect(
    dialog.getByRole("button", { name: "Delete account permanently" }),
  ).toBeDisabled();
  await dialog
    .getByLabel("Type reader-renamed to confirm")
    .fill("reader-renamed");
  await dialog
    .getByRole("button", { name: "Delete account permanently" })
    .click();
  await expect(dialog.getByLabel("Username", { exact: true })).toBeDisabled();
  await expect(
    dialog.getByLabel("Display name", { exact: true }),
  ).toBeDisabled();
  await expect(dialog.getByLabel("Account enabled")).toBeDisabled();
  await expect(
    dialog.getByRole("button", { name: "Cancel", exact: true }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  releaseDelete();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByRole("row", { name: /Reader/ })).toHaveCount(0);
  expect(writes).toEqual([
    "/api/auth/users/reader/providers/oidc",
    "/api/auth/users/reader/providers/oidc",
    "/api/auth/users/reader",
  ]);
});

test("provider registration roles appear only when relevant and retain their drafts", async ({
  page,
}) => {
  const values: Record<string, Record<string, unknown>> = {
    oidc: {
      enabled: false,
      label: "Identity provider",
      issuer: "",
      client_id: "",
      secret_set: false,
      redirect_uri: "https://dewarr.test/auth/callback",
      match_existing: "off",
      auto_register: false,
      default_role: "member",
      signing_algorithm: "RS256",
      authorization_endpoint: "",
      token_endpoint: "",
      userinfo_endpoint: "",
      jwks_uri: "",
      group_claim: "",
      group_scope: "",
      admin_group: "",
      member_group: "",
      viewer_group: "",
    },
    plex: {
      enabled: false,
      machine_id: "server-1",
      server_name: "Books",
      auto_register: false,
      default_role: "member",
    },
  };
  const saved: string[] = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "admin",
          username: "admin",
          role: "admin",
          display_name: "Admin",
          permissions: ["admin"],
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (path === "/api/auth/access") data = catalog;
    else if (path === "/api/auth/plex/pending") data = { servers: [] };
    else if (path.endsWith("/settings")) {
      const provider = path.includes("/oidc/") ? "oidc" : "plex";
      if (request.method() === "PUT") {
        const body = request.postDataJSON();
        expect(body.auto_register).toBe(false);
        expect(body.default_role).toBe("viewer");
        if (provider === "oidc") expect(body.group_claim).toBe("groups");
        values[provider] = { ...values[provider], ...body };
        saved.push(provider);
      }
      data = values[provider];
    }
    await route.fulfill({ json: data });
  });
  await page.goto("/settings#accounts");
  for (const [title, button] of [
    ["Identity provider", "Save identity provider"],
    ["Plex", "Save Plex sign-in"],
  ]) {
    const panel = page
      .locator("details.access-provider")
      .filter({ has: page.locator("summary > h2", { hasText: title }) });
    await panel.locator(":scope > summary > h2").click();
    const registration = panel.getByLabel("Create accounts on first sign-in");
    const access = panel.getByLabel("New account access");
    await expect(access).toHaveCount(0);
    await registration.check();
    await access.selectOption("viewer");
    await registration.uncheck();
    await expect(access).toHaveCount(0);
    await registration.check();
    await expect(access).toHaveValue("viewer");
    await registration.uncheck();
    if (title === "Identity provider") {
      await panel.getByText("Groups", { exact: true }).click();
      await panel.getByLabel("Group claim", { exact: true }).fill("groups");
      await expect(
        panel.getByRole("combobox", { name: "Default access", exact: true }),
      ).toHaveValue("viewer");
      await expect(
        panel.getByText(
          "Used when no configured group matches. Applies to existing accounts without a local password.",
        ),
      ).toBeVisible();
      await panel.getByLabel("Group claim", { exact: true }).fill("");
      await expect(
        panel.getByRole("combobox", { name: "Default access", exact: true }),
      ).toHaveCount(0);
      await panel.getByLabel("Group claim", { exact: true }).fill("groups");
      await expect(
        panel.getByRole("combobox", { name: "Default access", exact: true }),
      ).toHaveValue("viewer");
    }
    await panel.getByRole("button", { name: button, exact: true }).click();
  }
  await expect.poll(() => saved).toEqual(["oidc", "plex"]);
});
