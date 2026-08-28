const { test, expect } = require('./fixtures.cjs');

async function assertExamplePage(page) {
  await expect(page.getByRole('heading', { name: 'Example Domain' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Example Domain' })).toHaveText('Example Domain');
  await expect(page.locator('h1')).toHaveCount(1);
}

test('unmodified helper fixture browser and API suite', async ({ page, request, helperValue, webServer }) => {
  expect(helperValue).toBe('fixture-ready');
  expect(webServer).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/);
  await page.goto('/');
  await assertExamplePage(page);
  const response = await request.get('/');
  expect(response.status()).toBe(200);
  expect(await response.text()).toContain('Example Domain');
});
