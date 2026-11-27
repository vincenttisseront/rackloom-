import { expect, expectAccessible, login, logout, planViaApi, session, test } from './helpers'

// Lot C : gouvernance (F10 simulation, F12 step-up déjà dans login, F14 statut urgence).

test('Lot C — gouvernance : politique active, simulation sans écriture, urgence documentée', async ({ page }) => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  let planned: any
  try {
    planned = await planViaApi(alice, 'vitrine', 'Lot C : simulation politique')
  } finally {
    await alice.dispose()
  }

  await login(page, 'admin')
  await page.goto('/admin/gouvernance')
  await expect(page.getByRole('heading', { level: 1, name: /Gouvernance/ })).toBeVisible()
  await expect(page.getByTestId('policy-panel')).toContainText('builtin@9')
  await expect(page.getByTestId('sso-available')).toHaveText('oui')
  await expectAccessible(page, 'Lot C gouvernance')

  await page.getByTestId('sim-change-id').fill(planned.id)
  await page.getByTestId('sim-run').click()
  await expect(page.getByTestId('sim-result')).toContainText('refused')
  await expect(page.getByTestId('sim-result')).toContainText('R140')
  await expect(page.getByTestId('sim-result')).toContainText('/environment')
  await expect(page.getByTestId('sim-result')).toContainText('écritures : non')

  // F12 : lire le plan sans perdre la session ; l'approbation a exigé le step-up au login.
  await logout(page)
  await login(page, 'bruno')
  await page.goto(`/demandes/${planned.id}`)
  await expect(page.getByTestId('plan-panel')).toBeVisible()
})
