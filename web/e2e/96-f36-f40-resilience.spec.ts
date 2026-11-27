import { expect, login, planViaApi, session, test } from './helpers'

test('F36 — estimation file sans choix de runner', async ({ page }) => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  const planned = await planViaApi(alice, 'vitrine', 'F36 file')

  const est = await alice.call('GET', `/changes/${planned.id}/estimate`)
  expect(est.status).toBe(200)
  expect(est.body.model_used).toBe(false)
  expect(['idle', 'ready', 'waiting', 'running']).toContain(est.body.queue.status)
  expect(est.body.queue.runner_choice).not.toBe(true)

  await login(page, 'alice')
  await page.goto(`/demandes/${planned.id}`)
  await expect(page.getByTestId('delivery-estimate')).toBeVisible()
  await expect(page.getByTestId('estimate-queue')).toBeVisible()
  await alice.dispose()
})
