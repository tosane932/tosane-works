const { defineConfig } = require('playwright/test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

// Playwright loads config again in worker processes; all must share the webServer's directory.
const stateDir = process.env.E2E_STATE_DIR || fs.mkdtempSync(path.join(os.tmpdir(), 'bakery-register-e2e-'));
process.env.E2E_STATE_DIR = stateDir;
const port = 8765;

module.exports = defineConfig({
    testDir: '.', testMatch: '**/*.spec.cjs', workers: 1, retries: 0,
    timeout: 180000, expect: { timeout: 10000 },
    use: { baseURL: `http://127.0.0.1:${port}`, trace: 'retain-on-failure' },
    reporter: [['list']],
    webServer: {
        command: `${process.env.E2E_PYTHON || 'python'} tests_e2e/server.py`,
        cwd: path.join(__dirname, '..'),
        env: { E2E_STATE_DIR: stateDir, E2E_PORT: String(port) },
        url: `http://127.0.0.1:${port}/login`, reuseExistingServer: false,
        timeout: 120000,
    },
});
