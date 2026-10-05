import { expect, test } from "../fixtures";

test("local import browses a configured root and inspects exactly the chosen folder", async ({
  page,
}) => {
  const bodies: unknown[] = [];
  const browsed: string[] = [];
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    let data: unknown = { items: [], total: 0 };
    if (url.pathname === "/api/auth/me")
      data = {
        csrf_token: "qa",
        user: {
          id: "admin",
          username: "admin",
          role: "admin",
          display_name: "Reader",
        },
      };
    else if (url.pathname === "/api/setup/onboarding")
      data = { status: "completed" };
    else if (url.pathname === "/api/organization/download-roots")
      data = ["downloads"];
    else if (url.pathname === "/api/organization/download-files") {
      expect(url.searchParams.get("source_key")).toBe("downloads");
      const path = url.searchParams.get("path") || "";
      browsed.push(path);
      data = {
        path,
        parent: path ? "" : null,
        truncated: false,
        entries: path
          ? [
              {
                name: "book.epub",
                path: "Harbor/book.epub",
                kind: "file",
                size: 1000,
              },
            ]
          : [{ name: "Harbor", path: "Harbor", kind: "directory" }],
      };
    } else if (
      url.pathname === "/api/organization/inspections" &&
      route.request().method() === "POST"
    ) {
      bodies.push(route.request().postDataJSON());
      data = {
        id: "inspection",
        state: "queued",
        message: "Inspecting files",
        relative_path: "Harbor",
        source_key: "downloads",
      };
    } else if (url.pathname === "/api/organization/inspections") data = [];
    else if (url.pathname.endsWith("/inspections/inspection"))
      data = {
        id: "inspection",
        state: "queued",
        message: "Inspecting files",
        relative_path: "Harbor",
        source_key: "downloads",
      };
    await route.fulfill({ json: data });
  });
  await page.goto("/requests");
  await page.getByRole("link", { name: "Import local files" }).click();
  await page.getByRole("button", { name: "Browse files and folders" }).click();
  const dialog = page.getByRole("dialog", { name: "Choose completed files" });
  await dialog.getByRole("button", { name: /Harbor/ }).click();
  await expect(dialog).toContainText("book.epub");
  await dialog.getByRole("button", { name: "Choose this folder" }).click();
  await expect(page.getByLabel("Download path")).toHaveValue("Harbor");
  await expect(
    page.getByRole("button", { name: "Inspect files", exact: true }),
  ).toBeDisabled();
  await page
    .getByRole("checkbox", {
      name: "The download has finished and its files are no longer changing",
    })
    .check();
  await page
    .getByRole("button", { name: "Inspect files", exact: true })
    .click();
  await expect(page).toHaveURL(/inspection=inspection/);
  expect(browsed).toEqual(["", "Harbor"]);
  expect(bodies).toEqual([
    {
      source_key: "downloads",
      relative_path: "Harbor",
      completed_download: true,
    },
  ]);
  await page.route("**/api/organization/download-roots", (route) =>
    route.fulfill({ json: [] }),
  );
  await page.goto("/organization/inspections");
  await expect(
    page.getByRole("link", { name: "Open downloader settings" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Inspect files", exact: true }),
  ).toHaveCount(0);
});
