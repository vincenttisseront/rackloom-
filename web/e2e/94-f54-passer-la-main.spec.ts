import { expect, login, logout, planViaApi, press, session, test } from './helpers'

test('F54 — solliciter Bruno, répondre, hors périmètre 404', async ({ page }) => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  const planned = await planViaApi(alice, 'vitrine', 'F54 passer la main')

  await login(page, 'alice')
  await page.goto(`/demandes/${planned.id}`)
  await expect(page.getByTestId('handoff-panel')).toBeVisible()
  const select = page.getByTestId('handoff-candidate')
  await select.selectOption({ label: /Bruno/ })
  await page.getByTestId('handoff-question').fill('Le plan te paraît-il correct ?')
  await press(page.getByTestId('handoff-submit'))
  await expect(page.getByTestId('handoff-item')).toBeVisible()

  await logout(page)
  await login(page, 'bruno')
  await page.goto(`/demandes/${planned.id}`)
  await page.getByTestId('handoff-reply').fill('Oui, on peut approuver.')
  await press(page.getByTestId('handoff-reply-submit'))
  await expect(page.getByTestId('handoff-reply-text')).toContainText('approuver')

  const chloe = await session('chloe')
  expect((await chloe.call('GET', `/changes/${planned.id}/reviews`)).status).toBe(404)
  await chloe.dispose()
  await alice.dispose()
})
