import { expect, test } from "../fixtures";

test("link and unlink the current account with password confirmation", async ({
  page,
}) => {
  let linked = false;
  let failPassword = true;
  const passwords: string[] = [];
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    let data: unknown = [];
    const auth = {
      user: {
        id: "admin",
        username: "admin",
        display_name: "Admin",
        role: "admin",
        permissions: ["admin"],
        onboarding_status: "complete",
      },
      csrf_token: "fresh-csrf",
    };
    if (path === "/api/auth/me") data = auth;
    else if (path === "/api/auth/oidc")
      data = { enabled: true, label: "Test ID" };
    else if (path === "/api/auth/oidc/link") {
      if (request.method() === "POST") {
        passwords.push(request.postDataJSON().password);
        if (failPassword) {
          failPassword = false;
          return route.fulfill({
            status: 403,
            json: { detail: "Your local password is incorrect" },
          });
        }
        linked = true;
        data = { authorization_url: "/settings?oidc_linked=1#sign-in" };
      } else if (request.method() === "DELETE") {
        passwords.push(request.postDataJSON().password);
        linked = false;
        data = auth;
      } else
        data = {
          password_available: true,
          linked,
          issuer: linked ? "https://identity.example" : null,
        };
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#sign-in");
  await expect(
    page.getByText("Identity provider: Not linked", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Current password").fill("wrong password");
  await page
    .getByRole("button", { name: "Link identity provider", exact: true })
    .click();
  await expect(
    page.getByText("Your local password is incorrect", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Current password")).toHaveValue("");
  await page.getByLabel("Current password").fill("my local password");
  await page
    .getByRole("button", { name: "Link identity provider", exact: true })
    .click();
  await expect(
    page.getByText(
      "Identity provider linked. You can now sign in with either method.",
      { exact: true },
    ),
  ).toBeVisible();
  await page.getByLabel("Current password").fill("my local password");
  await page
    .getByRole("button", { name: "Unlink identity provider", exact: true })
    .click();
  await expect(
    page.getByText("Identity provider: Not linked", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("Current password")).toHaveValue("");
  expect(passwords).toEqual([
    "wrong password",
    "my local password",
    "my local password",
  ]);
});

test("administrator renames and disables an account with conflict feedback", async ({
  page,
}) => {
  const admin = {
    id: "admin",
    username: "admin",
    display_name: "Admin",
    role: "admin",
    active: true,
    permissions: ["admin"],
    access_label: "Administrator",
    library_ids: [],
    onboarding_status: "complete",
  };
  let reader = {
    ...admin,
    id: "reader",
    username: "reader",
    display_name: "Reader",
    role: "member",
    permissions: [],
    access_label: "Member",
  };
  let conflict = true;
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me") data = { user: admin, csrf_token: "test" };
    else if (path === "/api/auth/users") data = [admin, reader];
    else if (path === "/api/auth/access")
      data = { presets: [], permissions: [], roles: [] };
    else if (path === "/api/auth/users/reader/profile") {
      const body = route.request().postDataJSON();
      expect(body.expected_username).toBe("reader");
      expect(body.expected_active).toBe(true);
      if (conflict) {
        conflict = false;
        return route.fulfill({
          status: 409,
          json: { detail: "That username is already in use" },
        });
      }
      reader = {
        ...reader,
        username: body.username,
        display_name: body.display_name,
        active: body.active,
      };
      data = reader;
    } else if (path.endsWith("/settings")) data = { enabled: false };
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
  await dialog.getByLabel("Username", { exact: true }).fill("admin");
  await dialog
    .getByRole("button", { name: "Save account", exact: true })
    .click();
  await expect(
    dialog.getByText("That username is already in use", { exact: true }),
  ).toBeVisible();
  await dialog.getByLabel("Username", { exact: true }).fill("reader-old");
  await dialog
    .getByLabel("Display name", { exact: true })
    .fill("Former reader");
  await dialog.getByLabel("Account enabled", { exact: true }).uncheck();
  await dialog
    .getByRole("button", { name: "Save account", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText("Former reader", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Disabled account", { exact: true }),
  ).toBeVisible();
});
