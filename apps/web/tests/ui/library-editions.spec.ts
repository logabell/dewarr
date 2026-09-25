import { expect, test } from "../fixtures";

test("owned editions expose narrator, extra versions and separate files", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const id = "00000000-0000-0000-0000-000000000111";
  const aliasId = "00000000-0000-0000-0000-000000000112";
  const work = {
    id,
    title: "'Salem's Lot",
    authors: ["Stephen King"],
    language: "en",
    description: "A novel.",
    availability: {
      owned: true,
      ebook: true,
      audio: true,
      ebook_versions: 2,
      audio_versions: 2,
      primary_audio_narrators: ["Richard Nazarewich"],
      stale: false,
    },
  };
  const copies = ["audio", "ebook"].flatMap((medium) =>
    [0, 1].map((i) => ({
      id: `${medium}-${i}`,
      version_id: `${medium}-${i}`,
      medium,
      title: i ? "'Salem's Lot (older version)" : "'Salem's Lot",
      library_name: "My library",
      narrators: medium === "audio" && i === 0 ? ["Richard Nazarewich"] : [],
      formats: [medium === "audio" ? "m4b" : "epub"],
      state: "present",
      full_content: true,
      work_ids: [id],
      open_url: "https://library.example/item/1",
      files: [
        {
          path: `/library/${medium}/edition-${i}/book.${medium === "audio" ? "m4b" : "epub"}`,
          format: medium === "audio" ? "m4b" : "epub",
        },
      ],
    })),
  );
  await page.route("**/api/**", (route) => {
    const url = new URL(route.request().url());
    let data: unknown = {};
    if (url.pathname === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "viewer",
          username: "reader",
          display_name: "Reader",
        },
        csrf_token: "test",
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "completed" };
    else if (
      url.pathname === `/api/catalog/works/${id}` ||
      url.pathname === `/api/catalog/works/${aliasId}`
    )
      data = work;
    else if (
      url.pathname === `/api/metadata/works/${id}` ||
      url.pathname === `/api/metadata/works/${aliasId}`
    )
      data = { sources: [], fields: {}, versions: [], versions_total: 0 };
    else if (url.pathname.endsWith("/reader-match"))
      data = { status: "unmatched", book: null };
    else if (url.pathname.endsWith("/cover"))
      return route.fulfill({ status: 404 });
    else if (url.pathname === "/api/library/assets") {
      const medium = url.searchParams.get("medium");
      const items = copies.filter(
        (copy) => medium === "any" || copy.medium === medium,
      );
      data = { items, total: items.length, offset: 0, limit: 40 };
    }
    if (
      new URL(route.request().url()).pathname.includes(
        "/acquisition/preferences/",
      )
    )
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
  await page.goto(`/books/${aliasId}?tab=library&format=audio#library-copies`);
  await expect(page).toHaveURL(
    new RegExp(`/books/${id}\\?tab=library&format=audio#library-copies$`),
  );
  await expect(page.locator(".narrator-line")).toContainText(
    "Narrated by Richard Nazarewich",
  );
  await expect(page.locator(".reader-facts")).toContainText("English");
  const audioMore = page.getByRole("link", {
    name: "View 1 additional audiobook version",
    exact: true,
  });
  const ebookMore = page.getByRole("link", {
    name: "View 1 additional ebook version",
    exact: true,
  });
  await expect(audioMore).toHaveText("+1");
  await expect(ebookMore).toHaveText("+1");
  await audioMore.click();
  await expect(page).toHaveURL(/tab=library&format=audio/);
  const table = page.locator(".library-copy-table");
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await expect(table).toContainText("Narrated by Richard Nazarewich");
  await expect(table).toContainText("Narrator not supplied");
  await table
    .locator("tbody tr")
    .nth(1)
    .getByRole("button", { name: /file.*Location/ })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "/library/audio/edition-1/book.m4b",
  );
  await page.getByRole("dialog").getByRole("button", { name: /Close/ }).click();
  await ebookMore.click();
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await expect(table).not.toContainText("Audiobook");
  await table
    .locator("tbody tr")
    .first()
    .getByRole("button", { name: /file.*Location/ })
    .click();
  await expect(page.getByRole("dialog")).toContainText(
    "/library/ebook/edition-0/book.epub",
  );
  await page.getByRole("dialog").getByRole("button", { name: /Close/ }).click();
  expect(errors).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("library-editions.png"),
    fullPage: true,
  });
});
