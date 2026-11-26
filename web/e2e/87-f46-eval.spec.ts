/**
 * F46 : banc d'évaluation depuis Administration › Connecteurs.
 * Niveau : testé localement (adaptateur fictif). Aucun job créé.
 */
import { expect, test } from '@playwright/test'
import { expectAccessible, login, logout, press, setAssistant } from './helpers'

test('F46 — qualification nominale puis régression defaults_prod', async ({ page }) => {
  test.setTimeout(120_000)
  try {
    await login(page, 'admin')
    await page.goto('/admin/connecteurs')
    const card = page.getByTestId('assistant-admin')
    await expect(card).toBeVisible()

    await card.getByLabel('Adaptateur').selectOption('fake')
    await card.getByLabel('Scénario de démonstration').selectOption('nominal')
    await press(card.getByTestId('assistant-eval-run'))
    await expect(page.getByTestId('assistant-eval-verdict')).toHaveText('qualifié', { timeout: 30_000 })
    await expect(page.getByTestId('assistant-eval-report')).toContainText('fake:nominal')
    await expectAccessible(page, 'F46 eval passed')

    await card.getByLabel('Scénario de démonstration').selectOption('defaults_prod')
    await press(card.getByTestId('assistant-eval-run'))
    await expect(page.getByTestId('assistant-eval-verdict')).toHaveText('échoué', { timeout: 30_000 })
    await expect(page.getByTestId('assistant-eval-report')).toContainText('defaults_prod')
    await logout(page)
  } finally {
    await setAssistant('fake', 'nominal')
  }
})
