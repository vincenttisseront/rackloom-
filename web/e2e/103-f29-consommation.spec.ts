import { expect, expectAccessible, login, logout, session, test } from './helpers'

// F29 : panneau consommation — allocation/mesure/estimation ; aucun montant sans tarif.
test('F29 — consommation par équipe sans facture fictive', async ({ page }) => {
  test.setTimeout(120_000)
  const alice = await session('alice')
  try {
    const report = await alice.call('GET', '/projects/vitrine/consumption')
    expect(report.status).toBe(200)
    expect(report.body.billing).toBeNull()
    expect(report.body.billing_unavailable_reason).toBe('no_pricing_rates')
    expect(report.body.resources.length).toBeGreaterThan(0)
    expect(report.body.resources[0].allocated.kind).toBe('allocation')

    await login(page, 'alice')
    await page.goto('/projets/vitrine')
    const panel = page.getByTestId('consumption-panel')
    await expect(panel).toBeVisible()
    await expect(page.getByTestId('consumption-no-billing')).toContainText('données tarifaires absentes')
    await expect(page.getByTestId('consumption-reconciliation')).toBeVisible()
    await expectAccessible(page, 'F29 consommation')
    await logout(page)
  } finally {
    await alice.dispose()
  }
})
