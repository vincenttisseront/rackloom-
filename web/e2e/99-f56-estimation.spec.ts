import { expect, login, planViaApi, session, test } from './helpers'

test('F56 — estimation sans date garantie ni modèle', async ({ page }) => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  const planned = await planViaApi(alice, 'vitrine', 'F56 estimation')

  const est = await alice.call('GET', `/changes/${planned.id}/estimate`)
  expect(est.status).toBe(200)
  expect(est.body.model_used).toBe(false)
  expect(est.body.approval.guaranteed_delivery_at).toBeNull()
  expect(est.body.execution.label).toMatch(/non estimable/i)

  await login(page, 'alice')
  await page.goto(`/demandes/${planned.id}`)
  await expect(page.getByTestId('delivery-estimate')).toBeVisible()
  await expect(page.getByTestId('estimate-approval')).toContainText(/date|approb/i)
  await expect(page.getByTestId('estimate-no-model')).toBeVisible()
  await alice.dispose()
})
