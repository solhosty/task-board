import { defineConfig, devices } from '@playwright/test';

const port = Number(process.env.PLAYWRIGHT_PORT || 4173);
const baseURL = 'http://127.0.0.1:' + port;

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.e2e.ts',
  timeout: 30_000,
  use: { baseURL, trace: 'retain-on-failure' },
  webServer: {
    command: 'PYTHONDONTWRITEBYTECODE=1 python3 ../app.py --port ' + port,
    url: baseURL + '/api/bootstrap',
    timeout: 30_000,
    reuseExistingServer: !process.env.CI,
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], browserName: 'chromium' } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
});
