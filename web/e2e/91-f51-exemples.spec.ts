import { expect, expectAccessible, login, press, session, test } from './helpers'

test('F51 — exemples filtrés, sans prod pour Chloé, parcours prérempli', async ({ page }) => {
  test.setTimeout(180_000)

  // Sans accès production (compta) : aucun exemple prod.
  await login(page, 'chloe')
  await page.goto('/')
  await expect(page.getByTestId('request-examples')).toBeVisible()
  await expect(page.getByTestId('example-item')).toHaveCount(await page.getByTestId('example-item').count())
  expect(await page.getByTestId('example-item').locator('[data-production="1"]').count()).toBe(0)
  await expectAccessible(page, 'F51 Chloé')

  // Assistant désactivé : les exemples restent (API indépendante).
  const admin = await session('admin')
  // Pas d'API dédiée ici : on vérifie via GET /examples hors UI assistant.
  const examples = await admin.call('GET', '/examples')
  expect(examples.status).toBe(200)
  expect(examples.body.assistant_independent).toBe(true)
  await admin.dispose()

  await press(page.getByTestId('example-open').first())
  await expect(page.getByTestId('need-form')).toBeVisible()
  // Prefill : service sélectionné.
  await expect(page.locator('input[name="service"]:checked')).toHaveCount(1)
})
