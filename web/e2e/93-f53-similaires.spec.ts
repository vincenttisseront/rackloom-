import { expect, login, planViaApi, session, test } from './helpers'

test('F53 — demande similaire signalée ; hors périmètre invisible', async ({ page }) => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  const planned = await planViaApi(alice, 'vitrine', 'F53 similaire')

  await login(page, 'alice')
  await page.goto('/projets/vitrine/nouvelle')
  await page.locator('#svc-ubuntu-web').check()
  // Attendre l'aide similarité après sélection.
  await expect(page.getByTestId('similar-changes')).toBeVisible({ timeout: 15_000 })
  await expect(page.getByTestId('similar-item').filter({ hasText: planned.ref })).toBeVisible()

  const chloe = await session('chloe')
  const denied = await chloe.call('POST', '/projects/vitrine/changes/similar', {
    need: { schema: 'rackloom.need/v1', service: { template: 'ubuntu-web' }, environment: 'dev',
      confirm_production: false, inputs: { lifetime_until: '2099-01-01' } },
  })
  expect(denied.status).toBe(404)
  await chloe.dispose()
  await alice.dispose()
})
