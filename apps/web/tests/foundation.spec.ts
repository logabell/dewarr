import { expect, test } from "./fixtures";

// This project runs before the other browser projects and owns their shared
// synthetic account/integrations. Retired list-management UI is covered by the
// API integration suites; browser journeys below use the current navigation.
test("first account, saved books, library connection and worker", async ({
  page,
}) => {
  test.setTimeout(90_000);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Set up your library" }),
  ).toBeVisible();
  await page.getByLabel("Your name").fill("Test Reader");
  await page.getByLabel("Username", { exact: true }).fill("reader");
  await page
    .getByLabel("Password", { exact: true })
    .fill("browser test password");
  await page.getByRole("button", { name: "Create administrator" }).click();
  await page.getByRole("button", { name: "Finish later", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Sign out", exact: true }),
  ).toBeVisible();
  const auth = await (await page.request.get("/api/auth/me")).json();
  const headers = {
    Origin: "http://127.0.0.1:8001",
    "X-CSRF-Token": auth.csrf_token,
  };
  const command = async (
    method: "post" | "put",
    path: string,
    data: unknown = {},
  ) => {
    const response = await page.request[method](path, { headers, data });
    expect(response.ok(), `${path}: ${await response.text()}`).toBeTruthy();
    return response.json();
  };
  await page.goto("/library?view=saved");
  await page.getByRole("button", { name: "Add a title", exact: true }).click();
  await page.getByLabel("Title", { exact: true }).fill("The Synthetic Archive");
  await page.getByLabel("Author", { exact: true }).fill("Example Author");
  await page
    .getByLabel("Description")
    .fill("Synthetic browser-test catalog title.");
  await page.getByRole("button", { name: "Save title", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "The Synthetic Archive", exact: true }),
  ).toBeVisible();
  await page.goto("/settings#libraries");
  const libraries = page.getByRole("region", {
    name: "Libraries",
    exact: true,
  });
  await libraries
    .getByRole("button", { name: "Add server", exact: true })
    .click();
  await libraries
    .getByRole("button", { name: "Audiobookshelf", exact: true })
    .click();
  await libraries.getByLabel("Connection name").fill("Fixture ABS");
  await libraries.getByLabel(/^Server URL/).fill("http://127.0.0.1:13379/abs");
  await page
    .getByLabel("API token", { exact: true })
    .fill("browser-abs-fixture-token");
  await libraries
    .getByRole("button", { name: "Save connection", exact: true })
    .click();
  await libraries
    .getByRole("button", { name: "Test connection", exact: true })
    .click();
  await expect(libraries).toContainText("connected");
  const connections = await (
    await page.request.get("/api/integrations")
  ).json();
  const sync = await page.request.post(
    `/api/integrations/${connections[0].id}/sync`,
    {
      headers: { ...headers, "Idempotency-Key": "browser-baseline-sync" },
    },
  );
  expect(sync.status()).toBe(202);
  await expect
    .poll(
      async () =>
        (await (await page.request.get("/api/library/assets")).json()).total,
    )
    .toBeGreaterThan(0);
  await page.goto("/library");
  await expect(
    page.getByRole("heading", { name: "My Library", exact: true }),
  ).toBeAttached();
  await expect(page.locator(".book-card").first()).toBeVisible();
  await page.goto("/settings#logs");
  await page.getByRole("button", { name: "Check background worker" }).click();
  await expect(
    page.getByRole("region", { name: "Background activity", exact: true }),
  ).toContainText("completed", { timeout: 15_000 });

  // Stable fixtures for independent source/settings/recovery journeys.
  await command("post", "/api/downloaders", {
    name: "Fixture qBittorrent",
    base_url: "http://127.0.0.1:13379/qbit",
    username: "browser-qbit-user",
    password: "browser-qbit-password",
    category: "books",
    save_path: "/downloads",
    mappings: [{ download_root: "/downloads", source_key: "synthetic" }],
  });
  await command("put", "/api/sources/mam/connection", {
    base_url: "http://127.0.0.1:13379/mam",
    mam_id: "browser-mam-fixture",
    enabled: true,
  });
  await command("post", "/api/sources/mam/connection/test");
  await command("put", "/api/sources/prowlarr/connection", {
    base_url: "http://127.0.0.1:13379/prowlarr",
    api_key: "browser-prowlarr-key",
    enabled: true,
  });
  await command("post", "/api/sources/prowlarr/connection/test");
  await command("put", "/api/metadata/account", {
    token: "browser-hardcover-fixture",
    enabled: true,
  });
  const works = await (
    await page.request.get("/api/catalog/works?q=The%20Synthetic%20Archive")
  ).json();
  const work = works.items.find(
    (value: { title: string }) => value.title === "The Synthetic Archive",
  );
  const search = await page.request.post(
    `/api/catalog/works/${work.id}/source-searches`,
    {
      headers: {
        ...headers,
        "Idempotency-Key": "browser-baseline-source-search",
      },
      data: {},
    },
  );
  expect(search.ok(), await search.text()).toBeTruthy();
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get(
              `/api/catalog/works/${work.id}/source-searches/latest`,
            )
          ).json()
        )?.status,
      { timeout: 30_000 },
    )
    .toBe("completed");
  expect(errors).toEqual([]);
});
