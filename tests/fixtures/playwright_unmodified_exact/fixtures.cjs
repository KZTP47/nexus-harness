const base = require('@playwright/test');
const http = require('node:http');

exports.expect = base.expect;
exports.test = base.test.extend({
  helperValue: async ({}, use) => {
    await Promise.resolve();
    await use('fixture-ready');
  },
  webServer: async ({}, use) => {
    const server = http.createServer((_request, response) => response.end('contained fixture'));
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', resolve);
    });
    const address = server.address();
    await use(`http://127.0.0.1:${address.port}`);
    await new Promise(resolve => server.close(resolve));
  },
});
