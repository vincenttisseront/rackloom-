import { expect, login, press, session, test } from './helpers'

test('F52 — comparateur si deux profils ; sinon mode auto', async ({ page }) => {
  test.setTimeout(180_000)
  const alice = await session('alice')

  // Un seul profil publié → mode auto.
  const single = await alice.call('POST', '/projects/vitrine/comparisons', {
    need: { schema: 'rackloom.need/v1', service: { template: null }, environment: null,
      confirm_production: false, inputs: {} },
  })
  expect(single.status).toBe(200)
  expect(single.body.mode).toBe('auto')

  await login(page, 'alice')
  await page.goto('/projets/vitrine/nouvelle')
  await expect(page.getByTestId('need-form')).toBeVisible()
  // Avec un seul service : pas de grille de comparaison service.
  // Choisir le service puis comparer les niveaux.
  await press(page.locator('#svc-ubuntu-web'), 'Space')
  await press(page.locator('input[name="environment"]').first().or(page.getByLabel(/dev/i).first()).first())
  // Si environnement unique auto, déclencher size_tier compare via API.
  const tiers = await alice.call('POST', '/projects/vitrine/comparisons', {
    need: { schema: 'rackloom.need/v1', service: { template: 'ubuntu-web' }, environment: 'dev',
      confirm_production: false, inputs: {} },
  })
  expect(tiers.status).toBe(200)
  expect(tiers.body.mode).toBe('compare')
  expect(tiers.body.options.length).toBeGreaterThan(1)
  expect(tiers.body.omitted_criteria).toContain('cost')
  await alice.dispose()
})
