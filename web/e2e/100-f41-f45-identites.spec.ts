import { expect, expectAccessible, login, test } from './helpers'

// F41–F45 : page Identités (SCIM, revue, export d'audit, alertes TLS).

test('F41–F45 — identités et audit : SCIM, revue, export', async ({ page }) => {
  test.setTimeout(180_000)
  await login(page, 'admin')
  await page.goto('/admin/identites')
  await expect(page.getByRole('heading', { level: 1, name: /Identités et audit/ })).toBeVisible()
  await expect(page.getByTestId('scim-panel')).toBeVisible()
  await expect(page.getByTestId('review-panel')).toBeVisible()
  await expect(page.getByTestId('export-panel')).toBeVisible()
  await expect(page.getByTestId('tls-panel')).toBeVisible()
  await expectAccessible(page, 'F41–F45 identités')

  await page.getByTestId('scim-mint').click()
  await expect(page.getByTestId('scim-token')).toBeVisible()

  await page.getByTestId('review-prepare').click()
  await expect(page.getByTestId('notice')).toContainText(/Campagne préparée/)
  await expect(page.getByTestId('review-items')).toBeVisible()

  await page.getByTestId('export-create').click()
  await expect(page.getByTestId('notice')).toContainText(/Destination/)
  const run = page.locator('[data-testid^="export-run-"]').first()
  await expect(run).toBeVisible()
  await run.click()
  await expect(page.getByTestId('notice')).toContainText(/Export/)
})
