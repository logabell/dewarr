import { expect, test } from "../fixtures";

test("reader editions, primary selection and separation share a consistent group", async ({
  page,
}, testInfo) => {
  const id = "00000000-0000-0000-0000-000000000221";
  const sibling = "00000000-0000-0000-0000-000000000222";
  let selected = "audio-1";
  let separated = false;
  let revision = "1".repeat(64);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const work = (workId = id) => ({
    id: workId,
    title: "One Book",
    authors: ["Writer"],
    language: "en",
    availability: {
      owned: true,
      ebook: true,
      audio: !separated,
      ebook_versions: 1,
      audio_versions: separated ? 0 : 2,
      primary_audio_version_id: selected,
      primary_ebook_version_id: "ebook-1",
      primary_audio_narrators: [
        selected === "audio-1" ? "First Reader" : "Chosen Reader",
      ],
    },
  });
  const versions = [
    { id: "ebook-1", medium: "ebook", work_id: id, narrators: [] },
    {
      id: "audio-1",
      medium: "audio",
      work_id: sibling,
      narrators: ["First Reader"],
    },
    {
      id: "audio-2",
      medium: "audio",
      work_id: sibling,
      narrators: ["Chosen Reader"],
    },
  ].map((version) => ({
    ...version,
    owned: true,
    title: "One Book",
    language: "en",
    abridged: false,
    identifiers: {},
  }));
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    let data: unknown = {};
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "admin",
          username: "admin",
          display_name: "Admin",
          role: "admin",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === `/api/catalog/works/${id}`) data = work();
    else if (path === `/api/catalog/works/${id}/primary-edition`) {
      const body = route.request().postDataJSON();
      expect(body.expected_revision).toBe(revision);
      expect(body.medium).toBe("audio");
      selected = body.version_id;
      revision = "2".repeat(64);
      return route.fulfill({ status: 204 });
    } else if (path.endsWith("/grouping")) {
      if (route.request().method() === "PATCH") {
        expect(path).toBe(`/api/catalog/works/${sibling}/grouping`);
        expect(route.request().postDataJSON().separate).toBe(true);
        separated = true;
        return route.fulfill({ status: 204 });
      }
      data = {
        reason:
          "Grouped by matching title and authors with compatible language.",
        members: (separated ? [id] : [id, sibling]).map((workId) => ({
          work: work(workId),
          separate: false,
          revision,
        })),
      };
    } else if (path === `/api/metadata/works/${id}`) {
      expect([null, "display"]).toContain(url.searchParams.get("scope"));
      data = {
        sources: [],
        fields: {},
        items: versions,
        cover_choices: [],
        versions,
        versions_total: versions.length,
      };
    } else if (path.endsWith("/reader-match"))
      data = { status: "unmatched", book: null };
    else if (path.endsWith("/cover")) return route.fulfill({ status: 404 });
    else if (path === "/api/acquisition/profiles") data = [];
    else if (path === "/api/requests") data = { items: [], total: 0 };
    else if (path === "/api/requests/preview") {
      expect(route.request().postDataJSON().work_id).toBe(id);
      data = {
        specification: { mode: "audio" },
        targets: [
          {
            slot: "audio",
            state: "wanted",
            message: "Requested media is missing",
          },
        ],
        existing_copies: [
          {
            asset_id: "audio-copy",
            work_id: sibling,
            version_id: "audio-2",
            title: "One Book",
            medium: "audio",
            narrators: ["Chosen Reader"],
            state: "present",
            meets_requirements: true,
          },
        ],
      };
    } else if (path === "/api/library/assets")
      data = { items: [], total: 0, offset: 0, limit: 40 };
    else if (path.endsWith("/part-sets")) data = [];
    if (
      new URL(route.request().url()).pathname.includes(
        "/acquisition/preferences/",
      )
    )
      data = { effective: { desired_media: "both" } };
    return route.fulfill({ json: data });
  });
  await page.goto(`/books/${id}?tab=editions`);
  await expect(page.getByRole("table").locator("tbody tr")).toHaveCount(3);
  await expect(page.getByRole("table")).toContainText("Unabridged");
  await expect(
    page
      .getByRole("table")
      .getByRole("link", { name: "View library copies", exact: true }),
  ).toHaveCount(3);
  await page
    .getByRole("button", { name: "Use as primary", exact: true })
    .nth(2)
    .click();
  await expect(page.locator(".narrator-line")).toContainText("Chosen Reader");
  await expect(
    page.getByRole("table").locator("tbody tr").nth(2),
  ).toContainText("Primary edition");
  await page.screenshot({
    path: testInfo.outputPath("primary-edition.png"),
    fullPage: true,
  });
  await page.getByRole("tab", { name: "Library copies", exact: true }).click();
  await expect(
    page.getByText("How these editions are grouped", { exact: true }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: "Book metadata", exact: true }).click();
  await page
    .getByText("How these editions are grouped", { exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Confirm same book", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Keep this record separate", exact: true })
    .nth(1)
    .click();
  await expect(
    page.getByRole("button", {
      name: "Keep this record separate",
      exact: true,
    }),
  ).toHaveCount(1);
  await expect(page).toHaveURL(new RegExp(`/books/${id}\\?tab=manage$`));
  expect(errors).toEqual([]);
});
