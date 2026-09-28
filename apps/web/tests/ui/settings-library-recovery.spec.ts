import { expect, test, type Page } from "../fixtures";

async function setup(
  page: Page,
  connection: "connected" | "disabled" | "missing",
  options: { saved?: boolean; sharedSync?: boolean } = {},
) {
  const state = {
    status: "queued",
    syncs: 0,
    folders: !!options.saved,
    statusCode: 200,
    savedBody: null as Record<string, unknown> | null,
  };
  const syncId = () =>
    options.sharedSync ? "shared-sync" : `sync-${state.syncs}`;
  const destination = {
    id: "dest",
    library_id: "lib",
    workflow: "library",
    medium: "audio",
    root_key: "library-audio",
    backend_path: "/old-audiobooks",
    local_path: "/old-audiobooks",
    enabled: true,
    configured: true,
    revision: "one",
    server_kind: "audiobookshelf",
    mode: "hardlink",
    client_routes: [],
  };
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "user",
          role: "admin",
          display_name: "Reader",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path.startsWith("/api/acquisition/preferences/"))
      data = { effective: {}, inherited: {}, overrides: {}, revision: "one" };
    else if (path === "/api/integrations")
      data =
        connection === "missing"
          ? []
          : [
              {
                id: "abs",
                kind: "audiobookshelf",
                name: "Home ABS",
                base_url: "http://abs.test",
                status: "connected",
                enabled: connection === "connected",
                library_count: 1,
                book_count: 0,
              },
            ];
    else if (path === "/api/integrations/abs/sync") {
      state.syncs++;
      expect(route.request().method()).toBe("POST");
      expect(route.request().headers()["idempotency-key"]).toBeTruthy();
      data = {
        id: syncId(),
        status: "queued",
        message: "Waiting to sync Audiobookshelf",
      };
    } else if (path === `/api/integrations/abs/sync/${syncId()}`) {
      if (state.statusCode !== 200)
        return route.fulfill({
          status: state.statusCode,
          json: { detail: "Sync status unavailable" },
        });
      data = {
        id: syncId(),
        status: state.status,
        message:
          state.status === "failed"
            ? "Account access changed. Run a fresh sync."
            : "Reading library inventory",
      };
    } else if (path === "/api/organization/library-folders" && state.folders)
      data = [
        {
          library_id: "lib",
          library_name: "Audiobooks",
          server_name: "Home ABS",
          server_kind: "audiobookshelf",
          ebooks_allowed: false,
          audio_allowed: true,
          folders: ["/audiobooks"],
        },
      ];
    else if (path === "/api/organization/destinations" && options.saved)
      data = [destination];
    else if (path.endsWith("/automatic-import"))
      data = {
        requested_enabled: false,
        enabled: false,
        ready: false,
        generation: 1,
      };
    else if (path === "/api/organization/library-folders/audio") {
      state.savedBody = route.request().postDataJSON();
      data = { ...destination, ...state.savedBody };
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#libraries");
  await page
    .getByRole("button", {
      name: options.saved
        ? "Change audiobooks folder"
        : "Choose audiobooks folder",
    })
    .click();
  return state;
}

for (const statusCode of [404, 503]) {
  test(`a shared sync recovers after a ${statusCode} status response`, async ({
    page,
  }) => {
    const state = await setup(page, "connected", { sharedSync: true });
    const dialog = page.getByRole("dialog", {
      name: "Choose audiobooks folder",
    });
    state.statusCode = statusCode;
    await dialog.getByRole("button", { name: "Sync Home ABS" }).click();
    await expect(
      dialog.getByText("Could not check sync progress.", { exact: false }),
    ).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: "Sync Home ABS" }),
    ).toBeEnabled();
    // Retrying returns the same coalesced operation; Activity stays empty.
    state.statusCode = 200;
    state.status = "completed";
    state.folders = true;
    await dialog.getByRole("button", { name: "Sync Home ABS" }).click();
    await expect(
      dialog.getByText("/audiobooks", { exact: true }),
    ).toBeVisible();
    expect(state.syncs).toBe(2);
  });
}

test("an unavailable saved folder can be replaced by the only available folder", async ({
  page,
}) => {
  const state = await setup(page, "connected", { saved: true });
  const dialog = page.getByRole("dialog", { name: "Choose audiobooks folder" });
  await expect(
    dialog.getByText("The saved library folder is no longer available.", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "Save folder", exact: true }),
  ).toBeDisabled();
  await dialog
    .getByRole("combobox", { name: "Library", exact: true })
    .selectOption("lib|/audiobooks");
  await expect(
    dialog.getByRole("radio", { name: /Use Audiobookshelf path/ }),
  ).toBeChecked();
  await dialog
    .getByRole("button", { name: "Save folder", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  expect(state.savedBody).toMatchObject({
    library_id: "lib",
    backend_path: "/audiobooks",
    local_path: "/audiobooks",
    destination_id: "dest",
    expected_revision: "one",
  });
});

test("missing audiobook folders recover after sync, including a failed attempt", async ({
  page,
}) => {
  const state = await setup(page, "connected");
  const dialog = page.getByRole("dialog", { name: "Choose audiobooks folder" });
  await expect(
    dialog.getByText("No compatible library folders are available.", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    dialog.getByText(
      "Connect Audiobookshelf or Grimmory to choose a library folder.",
    ),
  ).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: /^Save/ })).toBeDisabled();
  await dialog.getByRole("button", { name: "Sync Home ABS" }).click();
  await expect(
    dialog.getByRole("button", { name: "Sync Home ABS" }),
  ).toBeDisabled();
  state.status = "failed";
  await expect(
    dialog.getByRole("status").filter({ hasText: "Account access changed" }),
  ).toBeVisible();
  state.status = "queued";
  await dialog.getByRole("button", { name: "Sync Home ABS" }).click();
  state.folders = true;
  state.status = "completed";
  await expect(dialog.getByText("/audiobooks", { exact: true })).toBeVisible();
  await expect(
    dialog.getByRole("radio", { name: /Use Audiobookshelf path/ }),
  ).toBeChecked();
  await expect(
    dialog.getByRole("button", { name: "Save folder", exact: true }),
  ).toBeEnabled();
  expect(state.syncs).toBe(2);
});

for (const connection of ["missing", "disabled"] as const) {
  test(`empty picker explains a ${connection} connection`, async ({ page }) => {
    await setup(page, connection);
    const dialog = page.getByRole("dialog", {
      name: "Choose audiobooks folder",
    });
    await expect(
      dialog.getByText(
        connection === "missing"
          ? "Connect Audiobookshelf or Grimmory to choose a library folder."
          : "Enable this connection in Libraries settings.",
        { exact: false },
      ),
    ).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: "Sync Home ABS" }),
    ).toHaveCount(0);
    await expect(
      dialog.getByRole("button", { name: "Refresh libraries" }),
    ).toBeEnabled();
  });
}
