const { defineConfig } = require('@playwright/test');

module.exports = defineConfig({
  workers: 1,
  use: {
    baseURL: process.env.NEXUS_TEST_EXACT_BASE_URL,
    browserName: 'chromium',
  },
});
