const { expect, test } = require("@playwright/test");

const NATIVE_THREAD_ID = "00000000-0000-4000-8000-000000000001";
const relayTaskPath = (taskId) => `/native/workflows/relay/tasks/${taskId}`;

test("Relay attachment dialog traps focus, makes background inert, and restores focus", async ({ page }) => {
  await page.goto(relayTaskPath(1));

  const opener = page.locator("[data-marvis-attach-open]");
  const composer = page.locator("[data-marvis-followup-composer]");
  const dialog = page.locator("[data-marvis-attachment-sheet]");
  const close = page.locator("[data-marvis-attachment-close]");

  await opener.focus();
  await opener.press("Enter");
  await expect(dialog).toBeVisible();
  await expect(close).toBeFocused();
  await expect(composer).toHaveJSProperty("inert", true);

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(opener).toBeFocused();
  await expect(composer).toHaveJSProperty("inert", false);
});

test("Relay work log closes on desktop and reopens only from its dedicated action", async ({ page }) => {
  await page.goto(relayTaskPath(1));

  const opener = page.locator("[data-marvis-open-log]");
  const drawer = page.locator("[data-marvis-work-log]");
  const close = page.locator("[data-marvis-close-log]");

  await expect(drawer).toBeVisible();
  await close.click();
  await expect(drawer).toBeHidden();
  await opener.click();
  await expect(drawer).toBeVisible();
});

test("Relay reconnects its event stream after an interrupted SSE connection", async ({ page }) => {
  let attempts = 0;
  await page.route("**/api/relay/tasks/2/events**", async (route) => {
    attempts += 1;
    await route.abort("connectionfailed");
  });

  await page.goto(relayTaskPath(2));
  await expect.poll(() => attempts, { timeout: 8_000 }).toBeGreaterThanOrEqual(2);
});

test("Relay input, attachment, and send never interrupt; only its dedicated control does", async ({ page }) => {
  let interruptRequests = 0;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      request.url().includes("/api/relay/tasks/3/interrupt")
    ) {
      interruptRequests += 1;
    }
  });

  await page.goto(relayTaskPath(3));
  const composer = page.locator("[data-marvis-followup-composer]");
  const input = composer.locator("textarea[name=text]");
  const attachment = composer.locator("[data-marvis-attach-open]");
  const interrupt = composer.locator("[data-marvis-interrupt-button]");

  await input.click();
  await attachment.click();
  const dialog = page.locator("[data-marvis-attachment-sheet]");
  await expect(dialog).toBeVisible();
  await expect(page.locator("[data-marvis-attachment-close]")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await input.fill("E2E relay follow-up");
  const sendRequest = page.waitForRequest(
    (request) =>
      request.method() === "POST" && request.url().includes("/api/relay/tasks/3/inputs")
  );
  await composer.locator("[data-marvis-submit]").click();
  await sendRequest;
  expect(interruptRequests).toBe(0);

  await expect(interrupt).toBeVisible();
  await expect(interrupt).toBeEnabled();
  const interruptResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes("/api/relay/tasks/3/interrupt") &&
      response.status() === 200
  );
  await interrupt.click();
  await interruptResponse;
  expect(interruptRequests).toBe(1);
});

test("Native send never doubles as interrupt, while the dedicated interrupt is reachable", async ({ page }) => {
  let interruptRequests = 0;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      request.url().includes(`/api/native/codex/sessions/${NATIVE_THREAD_ID}/interrupt`)
    ) {
      interruptRequests += 1;
    }
  });

  await page.goto(
    `/workers/4242/live?native_provider=codex&native_thread_id=${NATIVE_THREAD_ID}`
  );
  const prompt = page.locator("#prompt");
  const attachment = page.locator("#attachmentButton");
  const send = page.locator("#continue");
  const interrupt = page.locator("#interrupt");

  await prompt.click();
  await attachment.click();
  await expect(page.locator("#composerActionMenu")).not.toHaveClass(/closed/);
  expect(interruptRequests).toBe(0);

  await prompt.fill("E2E native message");
  const continueResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes(`/api/native/codex/sessions/${NATIVE_THREAD_ID}/continue`) &&
      response.status() === 200
  );
  await send.click();
  await continueResponse;
  expect(interruptRequests).toBe(0);

  await expect(page.locator(".dock-actions")).toBeVisible();
  await expect(interrupt).toBeVisible();
  await expect(send).toBeDisabled();
  await expect(send).toHaveAttribute("aria-label", "等待当前轮");
  const interruptResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes(`/api/native/codex/sessions/${NATIVE_THREAD_ID}/interrupt`) &&
      response.status() === 200
  );
  await interrupt.click();
  await interruptResponse;
  expect(interruptRequests).toBe(1);
});

test("Native 首页在模型目录失败时禁用新会话，重试成功后才恢复", async ({ page }) => {
  let startRequests = 0;
  page.on("request", (request) => {
    if (
      request.method() === "POST" &&
      request.url().includes("/api/native/codex/sessions/start")
    ) {
      startRequests += 1;
    }
  });
  await page.addInitScript(() => {
    localStorage.setItem(
      "wlcodexNativeModelSettings",
      JSON.stringify({ model: "stale-local-model", effort: "high", service_tier: "fast", version: 2 })
    );
  });
  await page.route("**/api/native/codex/models", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: "模型目录不可用", models: [] })
    })
  );

  await page.goto("/native/codex");
  const notice = page.locator("#modelCatalogNotice");
  const retry = page.locator("#modelCatalogRetry");
  const send = page.locator("#send");
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("模型目录不可用");
  await expect(notice).toContainText("无法创建新会话");

  await send.click();
  await page.locator("#prompt").fill("catalog must be fresh");
  await expect(send).toBeDisabled();
  expect(startRequests).toBe(0);

  await page.unroute("**/api/native/codex/models");
  const recoveredCatalog = page.waitForResponse(
    (response) =>
      response.url().includes("/api/native/codex/models") && response.status() === 200
  );
  await retry.click();
  await recoveredCatalog;
  await expect(notice).toBeHidden();
  await expect(send).toBeEnabled();
  const startResponse = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes("/api/native/codex/sessions/start") &&
      response.status() === 200
  );
  await send.click();
  await startResponse;
  expect(startRequests).toBe(1);
});

test("Native live 在空模型目录时继续会话但绝不发送陈旧模型", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem(
      "wlcodexNativeModelSettings",
      JSON.stringify({ model: "stale-local-model", effort: "high", service_tier: "fast", version: 2 })
    );
  });
  await page.route("**/api/native/codex/models", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ models: [] })
    })
  );
  await page.goto(
    `/workers/4242/live?native_provider=codex&native_thread_id=${NATIVE_THREAD_ID}`
  );

  const notice = page.locator("#modelCatalogNotice");
  const retry = page.locator("#modelCatalogRetry");
  const prompt = page.locator("#prompt");
  const send = page.locator("#continue");
  await expect(notice).toBeVisible();
  await expect(notice).toContainText("模型目录不可用");
  await expect(notice).toContainText("当前会话仍可继续");
  await expect(page.locator("#modelSelector")).toBeDisabled();

  await prompt.fill("continue without stale model");
  const continueRequest = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      request.url().includes(`/api/native/codex/sessions/${NATIVE_THREAD_ID}/continue`)
  );
  await send.click();
  const interruptionChoice = page.locator("#interruptionChoice");
  if (await interruptionChoice.isVisible()) {
    await page.locator("#queueChoice").click();
  }
  const request = await continueRequest;
  const body = JSON.parse(request.postData() || "{}");
  expect(JSON.stringify(body)).not.toContain("stale-local-model");
  expect(body).not.toHaveProperty("model");
  expect(body).not.toHaveProperty("effort");
  expect(body).not.toHaveProperty("service_tier");

  await page.unroute("**/api/native/codex/models");
  const recoveredCatalog = page.waitForResponse(
    (response) =>
      response.url().includes("/api/native/codex/models") && response.status() === 200
  );
  await retry.click();
  await recoveredCatalog;
  await expect(notice).toBeHidden();
});

test.describe("mobile touch basics", () => {
  test.use({ hasTouch: true, isMobile: true, viewport: { width: 390, height: 844 } });

  test("Relay attachment remains tappable with a 44px target and scaling enabled", async ({ page }) => {
    await page.goto(relayTaskPath(4));
    const viewport = page.locator('meta[name="viewport"]');
    const attachment = page.locator("[data-marvis-attach-open]");
    const bounds = await attachment.boundingBox();

    expect(await viewport.getAttribute("content")).not.toContain("user-scalable=no");
    expect(bounds).not.toBeNull();
    expect(bounds.width).toBeGreaterThanOrEqual(44);
    expect(bounds.height).toBeGreaterThanOrEqual(44);
    await attachment.tap();
    await expect(page.locator("[data-marvis-attachment-sheet]")).toBeVisible();
  });

  test("Relay work log is a modal dialog on mobile and restores focus after Escape", async ({ page }) => {
    await page.goto(relayTaskPath(4));

    const opener = page.locator("[data-marvis-open-log]");
    const drawer = page.locator("[data-marvis-work-log]");
    const close = page.locator("[data-marvis-close-log]");
    const phone = page.locator(".marvis-relay-phone");

    await expect(drawer).toBeHidden();
    await opener.focus();
    await opener.press("Enter");
    await expect(drawer).toBeVisible();
    await expect(drawer).toHaveAttribute("role", "dialog");
    await expect(drawer).toHaveAttribute("aria-modal", "true");
    await expect(close).toBeFocused();
    await expect(phone).toHaveJSProperty("inert", true);

    await page.keyboard.press("Shift+Tab");
    expect(
      await page.evaluate(
        () => Boolean(document.activeElement?.closest("[data-marvis-work-log]"))
      )
    ).toBe(true);

    await page.keyboard.press("Escape");
    await expect(drawer).toBeHidden();
    await expect(opener).toBeFocused();
    await expect(phone).toHaveJSProperty("inert", false);
  });

  test("Relay confirmation full page traps focus and restores its trigger", async ({ page }) => {
    await page.goto(relayTaskPath(5));

    const thumb = page.locator("[data-marvis-confirmation-open]");
    const confirmation = page.locator("[data-marvis-confirmation-page]");
    const close = confirmation.locator("[data-marvis-confirmation-close]");
    const phone = page.locator(".marvis-relay-phone");

    await expect(thumb).toBeVisible();
    await thumb.focus();
    await thumb.press("Enter");
    await expect(confirmation).toBeVisible();
    await expect(confirmation).toHaveAttribute("role", "dialog");
    await expect(confirmation).toHaveAttribute("aria-modal", "true");
    await expect(close).toBeFocused();
    await expect(phone).toHaveJSProperty("inert", true);

    await page.keyboard.press("Shift+Tab");
    expect(
      await page.evaluate(
        () => Boolean(document.activeElement?.closest("[data-marvis-confirmation-page]"))
      )
    ).toBe(true);

    await page.keyboard.press("Escape");
    await expect(confirmation).toBeHidden();
    await expect(thumb).toBeFocused();
    await expect(phone).toHaveJSProperty("inert", false);
  });
});
