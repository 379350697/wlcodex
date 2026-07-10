const { defineConfig } = require("@playwright/test");
const fs = require("node:fs");

const port = Number(process.env.WLCODEX_E2E_PORT || "43187");
const python = process.env.PYTHON || (
  fs.existsSync(".venv/bin/python") ? ".venv/bin/python" : "python3"
);

module.exports = defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  expect: {
    timeout: 5_000
  },
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  outputDir: "test-results",
  reporter: process.env.CI
    ? [["github"], ["html", { open: "never" }]]
    : "list",
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure"
  },
  webServer: {
    command: `${python} tests/e2e_server.py`,
    url: `http://127.0.0.1:${port}/health`,
    reuseExistingServer: false,
    timeout: 30_000,
    env: {
      ...process.env,
      PYTHONPATH: process.cwd(),
      PYTHONUNBUFFERED: "1",
      WLCODEX_E2E_PORT: String(port)
    }
  }
});
