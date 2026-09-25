import { expect, test } from "./fixtures";

for (const clientSetup of [
  "missing",
  "unmapped",
  "multiple",
  "partial failure",
]) {
  test(`a library folder can be saved with ${clientSetup} download clients`, async ({
    page,
  }) => {
    let destination: any = null;
    let clientReady = false;
    let requested = true;
    let active = false;
    let generation = 0;
    let activatedPreference: boolean | undefined;
    const writes: string[] = [];
    const probedClients: string[] = [];
    const downloader = {
      id: "qbit",
      kind: "qbittorrent",
      name: "qBittorrent",
      enabled: true,
      status: "connected",
      mappings_current: true,
      generation: 1,
    };
    await page.route("**/api/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (["PUT", "POST"].includes(route.request().method())) writes.push(path);
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
      else if (path === "/api/library/libraries")
        data = [{ id: "lib", name: "Books", accessible: true }];
      else if (path === "/api/organization/library-folders")
        data = [
          {
            library_id: "lib",
            library_name: "Books",
            server_kind: "audiobookshelf",
            ebooks_allowed: true,
            audio_allowed: true,
            folders: ["/data/ebooks"],
          },
        ];
      else if (path === "/api/downloaders")
        data = clientReady
          ? [downloader]
          : clientSetup === "missing"
            ? []
            : clientSetup === "unmapped"
              ? [{ ...downloader, mappings_current: false }]
              : [
                  downloader,
                  {
                    ...downloader,
                    id: "second",
                    kind: "sabnzbd",
                    name: "SABnzbd",
                  },
                ];
      else if (path.startsWith("/api/acquisition/preferences/"))
        data = { effective: {}, inherited: {}, overrides: {}, revision: "one" };
      else if (path === "/api/organization/destinations")
        data = destination ? [destination] : [];
      else if (path.endsWith("/automatic-import")) {
        if (route.request().method() === "PUT") {
          requested = route.request().postDataJSON().enabled;
          generation++;
        }
        data = {
          enabled: active && requested,
          requested_enabled: requested,
          ready: active && requested,
          can_enable: active,
          generation,
        };
      } else if (path.endsWith("/setup-probe")) {
        probedClients.push(route.request().postDataJSON().downloader_id);
        data = { id: "probe", status: "queued" };
      } else if (path === "/api/activity") {
        destination.publication_available = true;
        data = [
          {
            id: "probe",
            status:
              clientSetup === "partial failure" &&
              probedClients.at(-1) === "second"
                ? "failed"
                : "completed",
            message: "Download folder is not accessible",
          },
        ];
      } else if (path.endsWith("/activate")) {
        activatedPreference = route.request().postDataJSON().automatic;
        active = true;
        data = destination;
      } else if (path === "/api/organization/library-folders/ebook") {
        requested = route.request().postDataJSON().automatic;
        generation++;
        destination = {
          ...route.request().postDataJSON(),
          id: "dest",
          root_key: "library-ebook",
          medium: "ebook",
          mode: "hardlink",
          revision: "saved",
          publication_available: false,
        };
        data = destination;
      }
      return route.fulfill({ json: data });
    });
    await page.goto("/settings#libraries");
    await page.getByRole("button", { name: "Choose ebooks folder" }).click();
    const dialog = page.getByRole("dialog", { name: "Choose ebooks folder" });
    await expect(dialog.locator(".library-selection-summary")).toContainText(
      "/data/ebooks",
    );
    await expect(dialog.getByRole("radio")).toHaveCount(2);
    await expect(
      dialog.getByRole("combobox", { name: "Download client" }),
    ).toHaveCount(0);
    if (["multiple", "partial failure"].includes(clientSetup)) {
      await dialog
        .getByRole("button", { name: "Save & verify folder", exact: true })
        .click();
      if (clientSetup === "partial failure") {
        await expect(dialog.getByRole("alert")).toContainText(
          "SABnzbd: Download folder is not accessible",
          { timeout: 15_000 },
        );
      } else await expect(dialog).toHaveCount(0);
      expect(probedClients).toEqual(["qbit", "second"]);
      expect(activatedPreference).toBe(true);
      await expect(page.getByText("Hardlinks verified")).toBeVisible();
      return;
    }
    await expect(
      dialog.getByRole("checkbox", { name: "Import on completion" }),
    ).toBeChecked();
    await expect(
      dialog.getByRole("button", { name: "Save folder", exact: true }),
    ).toBeEnabled();
    await dialog
      .getByRole("button", { name: "Save folder", exact: true })
      .click();
    await expect(dialog).toHaveCount(0);
    const card = page.getByRole("region", {
      name: "Ebooks destination",
      exact: true,
    });
    await expect(card.getByText("/data/ebooks", { exact: true })).toBeVisible();
    await expect(
      card.getByText("Folder saved · verification required", { exact: true }),
    ).toBeVisible();
    await expect(
      card.getByText("No folder selected", { exact: true }),
    ).toHaveCount(0);
    expect(writes).toEqual(["/api/organization/library-folders/ebook"]);
    expect(destination.local_path).toBe("/data/ebooks");
    await expect(card.getByText(/On after verification/)).toBeVisible();
    await expect(
      card.getByRole("button", {
        name: "Enable automatic import",
        exact: true,
      }),
    ).toHaveCount(0);
    await expect(
      card.getByRole("link", { name: "Set up download client" }),
    ).toHaveAttribute("href", "/settings#downloaders");
    if (clientSetup === "missing") {
      await card
        .getByRole("button", { name: "Disable automatic import" })
        .click();
      await expect(
        card.getByText("Off · completed downloads stay in file review."),
      ).toBeVisible();
    }

    clientReady = true;
    await page.reload();
    await card
      .getByRole("button", { name: "Verify folder", exact: true })
      .click();
    const verification = page.getByRole("dialog", {
      name: "Verify ebooks folder",
    });
    await expect(
      verification.locator(".library-selection-summary"),
    ).toContainText("/data/ebooks");
    const automatic = clientSetup !== "missing";
    await expect(
      verification.getByRole("checkbox", { name: "Import on completion" }),
    ).toBeChecked({ checked: automatic });
    await verification
      .getByRole("button", {
        name: automatic ? "Verify & enable imports" : "Verify folder",
        exact: true,
      })
      .click();
    await expect(verification).toHaveCount(0);
    expect(activatedPreference).toBe(automatic);
    await expect(card.getByText("Hardlinks verified")).toBeVisible();
    await expect(
      card.getByText(
        automatic
          ? "On · matched downloads import automatically when complete."
          : "Off · completed downloads stay in file review.",
      ),
    ).toBeVisible();
  });
}

for (const mode of ["hardlink", "copy"]) {
  test(`two media rows choose ABS folders and verify ${mode} before activation`, async ({
    page,
  }, testInfo) => {
    const errors: string[] = [];
    const writes: { path: string; body: any }[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    let destination: any = null;
    let verified = false;
    let failure = false;
    let automaticEnabled = true;
    let activationFailure = mode === "copy";
    await page.route("**/api/**", (route) => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (!path.startsWith("/api/")) return route.fallback();
      if (path === "/api/organization/library-folders/browse") {
        const folder = url.searchParams.get("path");
        return route.fulfill({
          json: {
            path: folder,
            parent: folder === "/data" ? null : "/data",
            directories: !folder
              ? ["/data"]
              : folder === "/data"
                ? ["/data/library"]
                : folder === "/data/library"
                  ? ["/data/library/ebooks"]
                  : [],
            truncated: false,
          },
        });
      }
      const method = route.request().method();
      if (["PUT", "POST"].includes(method))
        writes.push({ path, body: route.request().postDataJSON() });
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
      else if (path === "/api/integrations")
        data = [
          {
            id: "abs",
            name: "Audiobookshelf",
            enabled: true,
            status: "connected",
            library_count: 2,
            book_count: 276,
            base_url: "http://library:13378",
            version: "2.36.1",
          },
        ];
      else if (path === "/api/library/libraries")
        data = [
          { id: "lib", name: "Books", accessible: true, granted_user_ids: [] },
        ];
      else if (path === "/api/organization/library-folders")
        data = [
          {
            library_id: "lib",
            library_name: "Books",
            server_name: "Audiobookshelf",
            ebooks_allowed: true,
            folders: ["/data/ebooks", "/data/audio"],
          },
          {
            library_id: "audio-only",
            library_name: "Audio only",
            server_name: "Audiobookshelf",
            ebooks_allowed: false,
            folders: ["/recordings"],
          },
        ];
      else if (path === "/api/organization/destinations")
        data = destination
          ? [{ ...destination, publication_available: !failure }]
          : [];
      else if (path === "/api/downloaders")
        data = [
          {
            id: "qbit",
            kind: "qbittorrent",
            name: "qBittorrent",
            enabled: true,
            status: "connected",
            mappings_current: true,
            generation: 1,
          },
        ];
      else if (path.endsWith("/automatic-import")) {
        if (method === "PUT")
          automaticEnabled = route.request().postDataJSON().enabled;
        data = {
          enabled: automaticEnabled,
          requested_enabled: automaticEnabled,
          ready: verified && automaticEnabled,
          can_enable: verified,
          generation: 1,
          message: automaticEnabled
            ? "Completed downloads import automatically"
            : "Automatic import is off",
        };
      } else if (path.startsWith("/api/acquisition/preferences/"))
        data = {
          effective: verified ? { ebook_destination_id: "dest" } : {},
          inherited: {},
          overrides: {},
          inherited_origins: {},
          revision: "one",
        };
      else if (path === "/api/organization/library-folders/ebook") {
        const body = route.request().postDataJSON();
        destination = {
          id: "dest",
          root_key: "library-ebook",
          medium: "ebook",
          enabled: true,
          mode: "hardlink",
          revision: "revision",
          ...body,
        };
        data = destination;
      } else if (path.endsWith("/setup-probe"))
        data = { id: "probe", status: "queued" };
      else if (path === "/api/activity") {
        if (!failure && mode === "copy")
          destination = {
            ...destination,
            mode: "copy",
            revision: "copy-revision",
          };
        data = [
          {
            id: "probe",
            status: failure ? "failed" : "completed",
            message: failure
              ? "Downloads and library must share a filesystem"
              : "Verified",
          },
        ];
      } else if (path.endsWith("/activate")) {
        if (activationFailure) {
          activationFailure = false;
          return route.fulfill({
            status: 409,
            json: { detail: "Activation temporarily unavailable; try again." },
          });
        }
        verified = true;
        data = { ...destination, publication_available: true };
      }
      return route.fulfill({ json: data });
    });
    await page.goto("/settings#storage");
    await page.evaluate(() =>
      document.documentElement.setAttribute("data-theme", "dark"),
    );
    await expect(page).toHaveURL(/#libraries$/);
    await expect(page.getByText("Library access", { exact: true })).toHaveCount(
      0,
    );
    await expect(
      page.getByRole("link", { name: "Saved profiles", exact: true }),
    ).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Choose ebooks folder" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "Choose audiobooks folder" }),
    ).toBeVisible();
    await expect(
      page.getByText("Default libraries & download routes"),
    ).toHaveCount(0);
    await expect(page.getByText("Server setup instructions")).toHaveCount(0);
    await page.screenshot({
      path: testInfo.outputPath("folders-desktop.png"),
      fullPage: true,
    });
    await page.getByRole("button", { name: "Choose ebooks folder" }).click();
    const dialog = page.getByRole("dialog", { name: "Choose ebooks folder" });
    await expect(dialog.getByText("/recordings", { exact: true })).toHaveCount(
      0,
    );
    const defaultFolder = dialog.getByRole("combobox", {
      name: "Library",
      exact: true,
    });
    await expect(defaultFolder).toHaveValue("lib|/data/ebooks");
    await expect(dialog.getByRole("radio")).toHaveCount(2);
    await defaultFolder.selectOption("lib|/data/audio");
    await expect(dialog.locator(".library-selection-summary")).toContainText(
      "/data/audio",
    );
    await defaultFolder.selectOption("lib|/data/ebooks");
    await expect(dialog.getByLabel("Custom library path")).not.toBeVisible();
    await expect(dialog.getByText("Audio only", { exact: true })).toHaveCount(
      0,
    );
    await dialog.getByRole("radio", { name: /^Use a custom path/ }).check();
    await dialog
      .getByRole("button", { name: "Browse folders", exact: true })
      .click();
    await expect(
      dialog.getByRole("region", { name: "Folders visible to Dewarr" }),
    ).toBeVisible();
    await dialog
      .locator(".mounted-folder-list")
      .getByRole("button", { name: "/data", exact: true })
      .dblclick();
    await dialog
      .locator(".mounted-folder-list")
      .getByRole("button", { name: "library", exact: true })
      .dblclick();
    await dialog.screenshot({
      path: testInfo.outputPath("library-folder-explorer.png"),
    });
    await dialog
      .locator(".mounted-folder-list")
      .getByRole("button", { name: "ebooks", exact: true })
      .dblclick();
    await dialog
      .getByRole("button", { name: "Select folder", exact: true })
      .click();
    await expect(dialog.getByLabel("Custom library path")).toHaveValue(
      "/data/library/ebooks",
    );
    await expect(defaultFolder).toHaveValue("lib|/data/ebooks");
    await dialog
      .getByRole("radio", { name: /Use Audiobookshelf path/ })
      .click();
    await expect(dialog.getByLabel("Custom library path")).not.toBeVisible();
    await dialog.getByRole("radio", { name: /^Use a custom path/ }).check();
    await expect(dialog.getByLabel("Custom library path")).toHaveValue(
      "/data/library/ebooks",
    );
    await dialog
      .getByRole("radio", { name: /Use Audiobookshelf path/ })
      .check();
    await expect(
      dialog.getByRole("button", { name: "Save & verify folder" }),
    ).toBeEnabled();
    await dialog.screenshot({
      path: testInfo.outputPath("folder-picker-desktop.png"),
    });
    failure = true;
    await dialog.getByRole("button", { name: "Save & verify folder" }).click();
    await expect(
      dialog.getByText("Downloads and library must share a filesystem"),
    ).toBeVisible();
    expect(writes.some((w) => w.path.endsWith("/activate"))).toBe(false);
    failure = false;
    await dialog.getByRole("button", { name: "Save & verify folder" }).click();
    if (mode === "copy") {
      await expect(
        dialog.getByText("Activation temporarily unavailable; try again."),
      ).toBeVisible();
      await dialog
        .getByRole("button", { name: "Save & verify folder" })
        .click();
    }
    await expect(dialog).toHaveCount(0);
    await expect(
      page.getByText(
        mode === "copy"
          ? "Copy mode · files are copied into this folder"
          : "Hardlinks verified",
      ),
    ).toBeVisible();
    if (mode === "copy") {
      expect(
        writes.filter((w) => w.path.endsWith("/ebook"))[2].body
          .expected_revision,
      ).toBe("copy-revision");
      expect(
        writes.filter((w) => w.path.endsWith("/activate"))[0].body
          .expected_revision,
      ).toBe("copy-revision");
    }
    expect(
      writes.filter((w) => w.path.endsWith("/ebook"))[1].body,
    ).toMatchObject({
      destination_id: "dest",
      expected_revision: "revision",
      backend_path: "/data/ebooks",
      local_path: "/data/ebooks",
    });
    expect(
      writes.find((w) => w.path.endsWith("/activate"))?.body.automatic,
    ).toBe(true);
    await page
      .getByRole("button", { name: "Disable automatic import", exact: true })
      .click();
    await expect(
      page.getByText("Off · completed downloads stay in file review.", {
        exact: true,
      }),
    ).toBeVisible();
    expect(
      writes.find((w) => w.path.endsWith("/automatic-import"))?.body.enabled,
    ).toBe(false);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({
      path: testInfo.outputPath("folders-mobile.png"),
      fullPage: true,
    });
    await page.getByRole("button", { name: "Change ebooks folder" }).click();
    await expect(dialog).toBeVisible();
    await expect(
      dialog.getByRole("checkbox", { name: "Import on completion" }),
    ).not.toBeChecked();
    await dialog.getByRole("radio", { name: /^Use a custom path/ }).check();
    await dialog
      .getByRole("button", { name: "Browse folders", exact: true })
      .click();
    await expect(
      dialog.getByRole("region", { name: "Folders visible to Dewarr" }),
    ).toBeVisible();
    await dialog.screenshot({
      path: testInfo.outputPath("library-folder-explorer-mobile.png"),
    });
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(
      dialog.getByRole("button", { name: "Browse folders", exact: true }),
    ).toBeFocused();
    await expect(dialog.getByLabel("Custom library path")).toHaveValue(
      "/data/ebooks",
    );
    await dialog.screenshot({
      path: testInfo.outputPath("folder-picker-mobile.png"),
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "Change ebooks folder" }),
    ).toBeFocused();
    expect(errors).toEqual([]);
  });
}

test("a Windows Audiobookshelf path asks for the Dewarr mount", async ({
  page,
}) => {
  const writes: { path: string; body: any }[] = [];
  let destination: any = null;
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith("/api/")) return route.fallback();
    if (path === "/api/organization/library-folders/browse") {
      const folder = url.searchParams.get("path");
      return route.fulfill({
        json: {
          path: folder,
          parent: folder === "/data" ? null : "/data",
          directories: !folder
            ? ["/data"]
            : folder === "/data"
              ? ["/data/library"]
              : folder === "/data/library"
                ? ["/data/library/ebooks"]
                : [],
          truncated: false,
        },
      });
    }
    const method = route.request().method();
    if (["PUT", "POST"].includes(method))
      writes.push({ path, body: route.request().postDataJSON() });
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
    else if (path === "/api/integrations")
      data = [
        {
          id: "abs",
          name: "Audiobookshelf",
          enabled: true,
          status: "connected",
          library_count: 1,
          book_count: 1,
          base_url: "http://library:13378",
          version: "2.36.1",
        },
      ];
    else if (path === "/api/library/libraries")
      data = [
        { id: "lib", name: "Books", accessible: true, granted_user_ids: [] },
      ];
    else if (path === "/api/organization/library-folders")
      data = [
        {
          library_id: "lib",
          library_name: "Books",
          server_name: "Audiobookshelf",
          ebooks_allowed: true,
          folders: ["D:/Books/Audiobooks"],
        },
      ];
    else if (path === "/api/organization/destinations")
      data = destination
        ? [{ ...destination, publication_available: true }]
        : [];
    else if (path === "/api/downloaders")
      data = [
        {
          id: "qbit",
          kind: "qbittorrent",
          name: "qBittorrent",
          enabled: true,
          status: "connected",
          mappings_current: true,
          generation: 1,
        },
      ];
    else if (path.endsWith("/automatic-import"))
      data = {
        enabled: true,
        ready: false,
        can_enable: false,
        generation: 1,
        message: "Completed downloads import automatically",
      };
    else if (path.startsWith("/api/acquisition/preferences/"))
      data = {
        effective: {},
        inherited: {},
        overrides: {},
        inherited_origins: {},
        revision: "one",
      };
    else if (path === "/api/organization/library-folders/ebook") {
      const body = route.request().postDataJSON();
      destination = {
        id: "dest",
        root_key: "library-ebook",
        medium: "ebook",
        enabled: true,
        mode: "hardlink",
        revision: "revision",
        publication_available: false,
        ...body,
      };
      data = destination;
    } else if (path.endsWith("/setup-probe"))
      data = { id: "probe", status: "queued" };
    else if (path === "/api/activity")
      data = [{ id: "probe", status: "completed", message: "Verified" }];
    else if (path.endsWith("/activate"))
      data = { ...destination, publication_available: true };
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#storage");
  await page.evaluate(() =>
    document.documentElement.setAttribute("data-theme", "dark"),
  );
  await page.getByRole("button", { name: "Choose ebooks folder" }).click();
  const dialog = page.getByRole("dialog", { name: "Choose ebooks folder" });
  await expect(
    dialog.getByText("D:/Books/Audiobooks", { exact: true }),
  ).toBeVisible();
  await expect(dialog.getByText("No compatible folders found")).toHaveCount(0);
  await expect(dialog.locator(".library-selection-summary")).toContainText(
    "D:/Books/Audiobooks",
  );
  await expect(dialog.getByRole("radio")).toHaveCount(2);
  await expect(
    dialog.getByRole("radio", { name: "Other path", exact: true }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("radio", { name: /^Use a custom path/ }),
  ).toBeChecked();
  await expect(
    dialog.getByRole("radio", { name: /Use Audiobookshelf path/ }),
  ).toBeDisabled();
  const mount = dialog.getByLabel("Custom library path");
  await expect(mount).toBeVisible();
  await expect(mount).toHaveValue("");
  await expect(
    dialog.getByRole("button", { name: "Save & verify folder" }),
  ).toBeDisabled();
  await mount.fill("D:/Books/Audiobooks");
  await dialog.getByRole("button", { name: "Save & verify folder" }).click();
  await expect(
    dialog.getByText(
      "Enter the absolute folder Dewarr has mounted, such as /data/audiobooks.",
    ),
  ).toBeVisible();
  await mount.fill("/data/audiobooks");
  await dialog.getByRole("button", { name: "Save & verify folder" }).click();
  await expect(dialog).toHaveCount(0);
  expect(writes.find((w) => w.path.endsWith("/ebook"))?.body).toMatchObject({
    backend_path: "D:/Books/Audiobooks",
    local_path: "/data/audiobooks",
  });
});

test("a UNC Audiobookshelf path is not used as the Dewarr mount", async ({
  page,
}) => {
  const writes: { path: string; body: any }[] = [];
  let destination: any = null;
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith("/api/")) return route.fallback();
    if (path === "/api/organization/library-folders/browse") {
      const folder = url.searchParams.get("path");
      return route.fulfill({
        json: {
          path: folder,
          parent: folder === "/data" ? null : "/data",
          directories: !folder
            ? ["/data"]
            : folder === "/data"
              ? ["/data/library"]
              : folder === "/data/library"
                ? ["/data/library/ebooks"]
                : [],
          truncated: false,
        },
      });
    }
    const method = route.request().method();
    if (["PUT", "POST"].includes(method))
      writes.push({ path, body: route.request().postDataJSON() });
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
    else if (path === "/api/integrations")
      data = [
        {
          id: "abs",
          name: "Audiobookshelf",
          enabled: true,
          status: "connected",
          library_count: 1,
          book_count: 1,
          base_url: "http://library:13378",
          version: "2.36.1",
        },
      ];
    else if (path === "/api/library/libraries")
      data = [
        { id: "lib", name: "Books", accessible: true, granted_user_ids: [] },
      ];
    else if (path === "/api/organization/library-folders")
      data = [
        {
          library_id: "lib",
          library_name: "Books",
          server_name: "Audiobookshelf",
          ebooks_allowed: true,
          folders: ["//media/share/Books"],
        },
      ];
    else if (path === "/api/organization/destinations")
      data = destination
        ? [{ ...destination, publication_available: true }]
        : [];
    else if (path === "/api/downloaders")
      data = [
        {
          id: "qbit",
          kind: "qbittorrent",
          name: "qBittorrent",
          enabled: true,
          status: "connected",
          mappings_current: true,
          generation: 1,
        },
      ];
    else if (path.endsWith("/automatic-import"))
      data = {
        enabled: true,
        ready: false,
        can_enable: false,
        generation: 1,
        message: "Completed downloads import automatically",
      };
    else if (path.startsWith("/api/acquisition/preferences/"))
      data = {
        effective: {},
        inherited: {},
        overrides: {},
        inherited_origins: {},
        revision: "one",
      };
    else if (path === "/api/organization/library-folders/ebook") {
      const body = route.request().postDataJSON();
      destination = {
        id: "dest",
        root_key: "library-ebook",
        medium: "ebook",
        enabled: true,
        mode: "hardlink",
        revision: "revision",
        publication_available: false,
        ...body,
      };
      data = destination;
    } else if (path.endsWith("/setup-probe"))
      data = { id: "probe", status: "queued" };
    else if (path === "/api/activity")
      data = [{ id: "probe", status: "completed", message: "Verified" }];
    else if (path.endsWith("/activate"))
      data = { ...destination, publication_available: true };
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#storage");
  await page.evaluate(() =>
    document.documentElement.setAttribute("data-theme", "dark"),
  );
  await page.getByRole("button", { name: "Choose ebooks folder" }).click();
  const dialog = page.getByRole("dialog", { name: "Choose ebooks folder" });
  await expect(dialog.locator(".library-selection-summary")).toContainText(
    "//media/share/Books",
  );
  await expect(dialog.getByRole("radio")).toHaveCount(2);
  await expect(
    dialog.getByRole("radio", { name: /^Use a custom path/ }),
  ).toBeChecked();
  await expect(
    dialog.getByRole("radio", { name: /Use Audiobookshelf path/ }),
  ).toBeDisabled();
  const mount = dialog.getByLabel("Custom library path");
  await expect(mount).toHaveValue("");
  await expect(
    dialog.getByRole("button", { name: "Save & verify folder" }),
  ).toBeDisabled();
  await mount.fill("//media/share/Books");
  await dialog.getByRole("button", { name: "Save & verify folder" }).click();
  await expect(
    dialog.getByText(
      "Enter the absolute folder Dewarr has mounted, such as /data/audiobooks.",
    ),
  ).toBeVisible();
  await mount.fill("/data/audiobooks");
  await dialog.getByRole("button", { name: "Save & verify folder" }).click();
  await expect(dialog).toHaveCount(0);
  expect(writes.find((w) => w.path.endsWith("/ebook"))?.body).toMatchObject({
    backend_path: "//media/share/Books",
    local_path: "/data/audiobooks",
  });
});

test("resuming folder setup shows both media choices", async ({ page }) => {
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (!path.startsWith("/api/")) return route.fallback();
    if (path === "/api/organization/library-folders/browse") {
      const folder = url.searchParams.get("path");
      return route.fulfill({
        json: {
          path: folder,
          parent: folder === "/data" ? null : "/data",
          directories: !folder
            ? ["/data"]
            : folder === "/data"
              ? ["/data/library"]
              : folder === "/data/library"
                ? ["/data/library/ebooks"]
                : [],
          truncated: false,
        },
      });
    }
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "admin",
          display_name: "Reader",
          onboarding_status: "deferred",
        },
        csrf_token: "test",
      };
    if (path === "/api/setup/onboarding")
      data = { status: "deferred", step: 4, skipped: [] };
    if (path === "/api/setup/readiness")
      data = { libraries: [], sources: [], downloaders: [] };
    if (path.startsWith("/api/acquisition/preferences/"))
      data = {
        effective: {},
        overrides: {},
        inherited: {},
        inherited_origins: {},
      };
    return route.fulfill({ json: data });
  });
  await page.goto("/onboarding");
  await expect(
    page.getByRole("heading", { name: "Library folders", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Choose ebooks folder" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Choose audiobooks folder" }),
  ).toBeVisible();
});
