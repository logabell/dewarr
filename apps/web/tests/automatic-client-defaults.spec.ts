import { expect, test } from "./fixtures";

test("client defaults are automatic per type and selectable when there are several", async ({
  page,
}, testInfo) => {
  const credentials = { username: "reader", password: "browser test password" };
  const headers = { Origin: "http://127.0.0.1:8001" };
  const bootstrap = await page.request.post("/api/auth/bootstrap", {
    headers,
    data: { ...credentials, display_name: "Test Reader" },
  });
  if (!bootstrap.ok()) {
    const login = await page.request.post("/api/auth/login", {
      headers,
      data: credentials,
    });
    expect(login.ok(), await login.text()).toBe(true);
  }
  const before = await (
    await page.request.get("/api/acquisition/preferences/personal")
  ).json();
  try {
    const torrent = {
      id: "10000000-0000-4000-8000-000000000001",
      name: "qBittorrent",
      protocol: "torrent",
      generation: 1,
      ready: true,
    };
    const nzb = {
      ...torrent,
      id: "10000000-0000-4000-8000-000000000002",
      name: "SABnzbd",
      protocol: "nzb",
    };
    const soulseek = {
      ...torrent,
      id: "10000000-0000-4000-8000-000000000003",
      name: "Soulseek",
      protocol: "soulseek",
    };
    const second = {
      ...torrent,
      id: "10000000-0000-4000-8000-000000000004",
      name: "Transmission",
    };
    const audio = {
      id: "20000000-0000-4000-8000-000000000001",
      library_id: "30000000-0000-4000-8000-000000000001",
      name: "Audiobooks",
      medium: "audio",
      ready: true,
      automatic_import_ready: true,
      revision: "a".repeat(64),
      source_key: "downloads",
    };
    const ebook = {
      ...audio,
      id: "20000000-0000-4000-8000-000000000002",
      name: "Ebooks",
      medium: "ebook",
    };
    const secondAudio = {
      ...audio,
      id: "20000000-0000-4000-8000-000000000003",
      name: "Family audiobooks",
    };
    let destinations = [audio, ebook];
    let clients = [torrent, nzb, soulseek];
    await page.route("**/api/acquisition/selections/options", (route) =>
      route.fulfill({ json: { downloaders: clients, destinations } }),
    );
    if (bootstrap.ok()) {
      await page.goto("/");
      await page
        .getByRole("button", { name: "Finish later", exact: true })
        .click();
    }
    await page.goto("/settings#preferences");
    const panel = page.getByRole("region", {
      name: "Download defaults",
      exact: true,
    });
    const expand = () =>
      panel
        .getByText("Downloader and destination defaults", { exact: true })
        .click();
    await expand();
    const torrentDefault = panel.getByLabel("Default torrent downloader", {
      exact: true,
    });
    const nzbDefault = panel.getByLabel("Default Usenet downloader", {
      exact: true,
    });
    const audioDefault = panel.getByLabel("Default audiobook destination", {
      exact: true,
    });
    const ebookDefault = panel.getByLabel("Default ebook destination", {
      exact: true,
    });
    await expect(audioDefault).toHaveValue(audio.id);
    await expect(audioDefault).toBeDisabled();
    await expect(ebookDefault).toHaveValue(ebook.id);
    await expect(ebookDefault).toBeDisabled();
    await expect(
      panel.getByText(
        "Uses your audiobook folder from Libraries. No separate destination setup needed.",
      ),
    ).toBeVisible();
    await expect(torrentDefault).toHaveValue(torrent.id);
    await expect(torrentDefault).toBeDisabled();
    await expect(nzbDefault).toHaveValue(nzb.id);
    await expect(nzbDefault).toBeDisabled();
    await expect(
      panel.getByText("Used automatically · your only torrent client.", {
        exact: true,
      }),
    ).toBeVisible();
    await expect(
      panel.getByRole("button", {
        name: "Save download defaults",
        exact: true,
      }),
    ).toBeDisabled();
    await page.reload();
    await expand();
    await expect(torrentDefault).toHaveValue(torrent.id);
    clients = [...clients, second];
    await page.reload();
    await expand();
    await expect(torrentDefault).toBeEnabled();
    await expect(torrentDefault).toHaveValue("");
    await expect(nzbDefault).toHaveValue(nzb.id);
    await expect(nzbDefault).toBeDisabled();
    await torrentDefault.selectOption(second.id);
    await panel
      .getByRole("button", { name: "Save download defaults", exact: true })
      .click();
    await expect(panel.getByRole("status")).toContainText(
      "Download defaults saved",
    );
    await page.reload();
    await expand();
    await expect(torrentDefault).toHaveValue(second.id);
    await expect(nzbDefault).toHaveValue(nzb.id);
    destinations = [...destinations, secondAudio];
    await page.reload();
    await expand();
    await expect(audioDefault).toBeEnabled();
    await expect(audioDefault).toHaveValue("");
    await expect(ebookDefault).toHaveValue(ebook.id);
    await expect(ebookDefault).toBeDisabled();
    await audioDefault.selectOption(secondAudio.id);
    await panel
      .getByRole("button", { name: "Save download defaults", exact: true })
      .click();
    await expect(panel.getByRole("status")).toContainText(
      "Download defaults saved",
    );
    await page.reload();
    await expand();
    await expect(audioDefault).toHaveValue(secondAudio.id);
    await page.screenshot({
      path: testInfo.outputPath("protocol-defaults.png"),
      fullPage: true,
    });
  } finally {
    const current = await (
      await page.request.get("/api/acquisition/preferences/personal")
    ).json();
    const auth = await (await page.request.get("/api/auth/me")).json();
    const restored = await page.request.put(
      "/api/acquisition/preferences/personal",
      {
        headers: { ...headers, "X-CSRF-Token": auth.csrf_token },
        data: {
          overrides: before.overrides,
          expected_revision: current.revision,
        },
      },
    );
    expect(restored.ok(), await restored.text()).toBe(true);
  }
});
