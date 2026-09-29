import { expect, test } from "../fixtures";

test("unreadable ebooks explain the file failure and offer reinspection", async ({
  page,
}) => {
  let retried = false;
  await page.route("**/api/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    let data: unknown = { items: [], total: 0 };
    if (path === "/api/auth/me")
      data = {
        user: {
          id: "reader",
          role: "admin",
          username: "reader",
          display_name: "Test Reader",
          onboarding_status: "completed",
          permissions: [],
        },
        csrf_token: "test",
      };
    else if (path === "/api/setup/onboarding") data = { status: "completed" };
    else if (
      path.endsWith("/inspections/unreadable") ||
      path.endsWith("/inspections/unreadable/retry")
    ) {
      if (path.endsWith("/retry")) retried = true;
      data = {
        id: "unreadable",
        state: retried ? "queued" : "ready",
        relative_path: "book.epub",
        plan_id: null,
        snapshot: {
          revision: "original",
          groups: [],
          files: [
            {
              path: "book.epub",
              state: "held",
              medium: null,
              reason: "Invalid media metadata",
            },
          ],
        },
        download: {
          work_id: "book",
          title: "The Harbor",
          authors: ["Alex Morgan"],
          medium: "ebook",
          state: retried ? "inspecting" : "held",
          can_retry: !retried,
          file_conflicts: [],
        },
      };
    } else if (path.endsWith("/grouping"))
      data = {
        revision: "original",
        content: {
          groups: [],
          excluded: [{ path: "book.epub", reason: "Invalid media metadata" }],
        },
      };
    else if (
      path.endsWith("/destinations") ||
      path.endsWith("/download-roots") ||
      path.endsWith("/inspections")
    )
      data = [];
    return route.fulfill({ json: data });
  });
  await page.goto("/organization/inspections?inspection=unreadable");
  await expect(page.getByRole("heading", { name: "The Harbor" })).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Download next step" }),
  ).toContainText("Invalid media metadata");
  await expect(
    page.getByText(/These files may contain several books/),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "View files" }).click();
  const files = page.getByRole("dialog", { name: "Downloaded files" });
  await expect(files).toContainText("Needs attention");
  await expect(files).toContainText("Invalid media metadata");
  await expect(files).not.toContainText("Extra file");
  await files.getByRole("button", { name: "Close downloaded files" }).click();
  await page.getByRole("button", { name: "Check files again" }).click();
  await expect.poll(() => retried).toBe(true);
  await expect(
    page.getByRole("heading", { name: "Adding your book to the library" }),
  ).toBeVisible();
});
