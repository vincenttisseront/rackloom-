import { expect, expectAccessible, login, logout, planViaApi, session, test } from './helpers'

// F05 : le panneau « Détails du plan » expose la capacité (mesure, pas réservation).
test('F05 — capacité affichée sans réservation', async ({ page }) => {
  test.setTimeout(180_000)
  const alice = await session('alice')
  try {
    const planned = await planViaApi(alice, 'vitrine', 'F05 : capacité')
    expect(planned.plan.details.capacity).toBeTruthy()
    expect(planned.plan.details.capacity.reservation).toBe(false)
    expect(planned.plan.details.capacity.resources.length).toBeGreaterThan(0)

    await login(page, 'alice')
    await page.goto(`/demandes/${planned.id}`)
    const details = page.getByTestId('plan-details')
    await expect(details).toBeVisible()
    await details.locator('summary').click()
    await expect(page.getByTestId('capacity-panel')).toBeVisible()
    await expect(page.getByTestId('capacity-panel')).toContainText('pas une réservation')
    await expectAccessible(page, 'F05 capacité')
    await logout(page)
  } finally {
    await alice.dispose()
  }
})
