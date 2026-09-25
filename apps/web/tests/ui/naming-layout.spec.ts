import { expect, test } from "../fixtures";

test("naming lanes stay on one line and preserve independent format settings", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const defaults = {
    layout: "conventional",
    rename_files: true,
    audio_folder:
      "{author}/[{series}/][{sequence} - ][{recording_year} - ]{title}[ - {narrator}]",
    ebook_folder:
      "{author}/[{series}/][{sequence} - ][{edition_year} - ]{title}[ - {edition}]",
    audio_filename: "[{disc}-][{track} - ]{title}",
    ebook_filename: "{title}",
    merge_mp3_chapters: false,
  };
  let profile = { ...defaults };
  let revision = 1;
  const previews: (typeof profile)[] = [];
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (!path.startsWith("/api/")) return route.fallback();
    let data: unknown = [];
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "admin",
          display_name: "Reader",
          onboarding_status: "complete",
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (path === "/api/organization/settings") {
      if (route.request().method() === "PUT") {
        profile = route.request().postDataJSON().profile;
        revision++;
      }
      data = { profile, revision: String(revision) };
    } else if (path === "/api/organization/defaults") data = defaults;
    else if (path === "/api/organization/destinations")
      data = ["ebook", "audio"].map((medium) => ({
        id: medium,
        medium,
        root_key: `library-${medium}`,
        backend_path: `/media/${medium}`,
        library_id: medium,
        publication_available: true,
        mode: "hardlink",
        revision: "one",
      }));
    else if (path.startsWith("/api/acquisition/preferences/"))
      data = {
        effective: {
          ebook_destination_id: "ebook",
          audio_destination_id: "audio",
        },
        inherited: {},
        overrides: {},
      };
    else if (path === "/api/organization/preview") {
      const draft = route.request().postDataJSON().profile;
      previews.push(draft);
      if (draft.audio_folder.includes(".."))
        return route.fulfill({
          status: 422,
          json: { detail: "Relative traversal is not a naming segment" },
        });
      data = {
        items: ["ebook", "audio"].map((medium) => ({
          medium,
          state: "ready",
          files: [
            {
              destination: `${medium === "audio" ? "audiobooks" : "ebooks"}/J. K. Rowling/Harry Potter/1 - Harry Potter and the Philosopher’s Stone/${medium === "audio" ? "01 - Title.mp3" : "Title.epub"}`,
            },
          ],
        })),
      };
    }
    return route.fulfill({ json: data });
  });
  await page.goto("/settings#naming");
  const lane = page.getByRole("list", { name: "Folder token order" });
  await expect(lane).toBeVisible();
  await expect(page.getByLabel("Example destination path")).toContainText(
    "/media/ebook",
  );
  await expect(page.getByLabel("Example destination path")).toContainText(
    "J. K. Rowling",
  );
  await expect(
    page.getByRole("button", { name: "Recommended", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  const assertOneLine = async () => {
    const positions = await lane
      .locator(":scope > li")
      .evaluateAll((items) =>
        items.map((item) => Math.round(item.getBoundingClientRect().top)),
      );
    expect(new Set(positions).size).toBe(1);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  };
  await assertOneLine();
  await page.screenshot({
    path: testInfo.outputPath("naming-desktop.png"),
    fullPage: true,
  });
  await expect(
    page.getByRole("button", { name: "Ebook", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  expect(
    await lane
      .locator("li")
      .first()
      .evaluate((node) => node.getBoundingClientRect().height),
  ).toBeLessThanOrEqual(34);
  await expect(
    page.getByText("Customize filename template", { exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "Audiobook", exact: true }).click();
  await expect(
    page.getByRole("checkbox", { name: /Merge MP3 chapters into one M4B/ }),
  ).toHaveCount(0);
  const sequence = lane.getByRole("button", {
    name: "Reorder [{sequence} - ] in Folder token order",
    exact: true,
  });
  await sequence.focus();
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(() => previews.at(-1)?.audio_folder)
    .toBe(
      "{author}/[{series}/][{recording_year} - ][{sequence} - ]{title}[ - {narrator}]",
    );
  await sequence.dragTo(
    lane.locator("li").filter({ hasText: "Recording year" }),
  );
  await expect(lane.locator(".naming-token-label")).toHaveText([
    "Author",
    "Series",
    "Sequence",
    "Recording year",
    "Title",
    "Narrator",
  ]);
  await page.getByRole("checkbox", { name: "Language", exact: true }).click();
  await page.getByRole("button", { name: "Ebook", exact: true }).click();
  await page.getByRole("button", { name: "By author", exact: true }).click();
  await expect(page.getByLabel("Example destination path")).toContainText(
    "/media/ebook",
  );
  await page.getByRole("button", { name: "Save naming settings" }).click();
  expect(profile.ebook_folder).toBe("{author}/{title}");
  expect(profile.audio_folder).toBe(`${defaults.audio_folder}[ - {language}]`);
  expect(profile.merge_mp3_chapters).toBe(false);
  await page.reload();
  await page.getByRole("button", { name: "Audiobook", exact: true }).click();
  await expect(
    page.getByRole("checkbox", { name: "Language", exact: true }),
  ).toBeChecked();
  await page
    .getByRole("button", { name: "Sequence title", exact: true })
    .click();
  await expect
    .poll(() => previews.at(-1)?.audio_folder)
    .toBe("{author}/[{series}/][{sequence} ]{title}");
  await expect
    .poll(() => previews.at(-1)?.audio_filename)
    .toBe(defaults.audio_filename);
  await page
    .getByRole("button", { name: "Use number, series, and year", exact: true })
    .click();
  await expect
    .poll(() => previews.at(-1)?.audio_filename)
    .toBe("[{sequence} - ][{series} - ]{title}[ ({year})]");
  await page
    .getByRole("button", { name: "Adjust dashes and folders", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "Join Sequence in Folder token order" })
    .selectOption("dash");
  await expect
    .poll(() => previews.at(-1)?.audio_folder)
    .toBe("{author}/[{series}/][{sequence} - ]{title}");
  await page
    .getByRole("combobox", { name: "Join Sequence in Folder token order" })
    .selectOption("space");
  await page
    .getByRole("combobox", { name: "Join Year in Filename token order" })
    .selectOption("dash");
  await expect
    .poll(() => previews.at(-1)?.audio_filename)
    .toBe("[{sequence} - ][{series} - ]{title}[ - {year}]");
  await page.setViewportSize({ width: 390, height: 844 });
  await assertOneLine();
  expect(
    await lane.evaluate((node) => node.scrollWidth > node.clientWidth),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("naming-mobile.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});
