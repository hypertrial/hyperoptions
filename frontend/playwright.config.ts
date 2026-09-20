import { defineConfig, devices } from "@playwright/test"

const frontendPort = 5173
const backendPort = 8000

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  workers: 1,
  timeout: 30_000,
  use: {
    baseURL: `http://127.0.0.1:${frontendPort}`,
    trace: "off",
    permissions: ["clipboard-read", "clipboard-write"],
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: "uv run python -m options_api.e2e_server",
      cwd: "../backend",
      url: `http://127.0.0.1:${backendPort}/api/health`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 5173",
      url: `http://127.0.0.1:${frontendPort}`,
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
})
