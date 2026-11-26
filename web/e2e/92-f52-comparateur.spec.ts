import { expect, login, session, test } from './helpers'

test('F52 — comparateur si deux profils ; sinon mode auto', async ({ page }) => {
  test.setTimeout(180_000)
  const alice = await session('alice')

  // Un seul profil publié → mode auto (pas de grille service).
  const single = await alice.call('POST', '/projects/vitrine/comparisons', {
    need: {
      schema: 'rackloom.need/v1',
      service: { template: null },
      environment: null,
      confirm_production: false,
      inputs: {},
    },
  })
  expect(single.status).toBe(200)
  expect(single.body.mode).toBe('auto')
  expect(single.body.omitted_criteria).toContain('cost')

  // Niveaux : plusieurs size_tier pour ubuntu-web / dev → compare ; coût omis.
  const tiers = await alice.call('POST', '/projects/vitrine/comparisons', {
    need: {
      schema: 'rackloom.need/v1',
      service: { template: 'ubuntu-web' },
      environment: 'dev',
      confirm_production: false,
      inputs: {},
    },
  })
  expect(tiers.status).toBe(200)
  expect(tiers.body.mode).toBe('compare')
  expect(tiers.body.dimension).toBe('size_tier')
  expect(tiers.body.options.length).toBeGreaterThan(1)
  expect(tiers.body.recommendation?.key).toBeTruthy()
  expect(tiers.body.omitted_criteria).toContain('cost')

  // Choix déjà figés → pas de comparateur.
  const fixed = await alice.call('POST', '/projects/vitrine/comparisons', {
    need: {
      schema: 'rackloom.need/v1',
      service: { template: 'ubuntu-web' },
      environment: 'dev',
      confirm_production: false,
      inputs: { size_tier: 'test_leger', lifetime_until: '2099-01-01' },
    },
  })
  expect(fixed.status).toBe(200)
  expect(fixed.body.mode).toBe('auto')
  expect(fixed.body.options).toEqual([])

  // Parcours formulaire toujours praticable (env unique : pas de select environnement).
  await login(page, 'alice')
  await page.goto('/projets/vitrine/nouvelle')
  await expect(page.getByTestId('need-form')).toBeVisible()
  await page.locator('#svc-ubuntu-web').check()
  await expect(page.locator('#svc-ubuntu-web')).toBeChecked()

  await alice.dispose()
})
