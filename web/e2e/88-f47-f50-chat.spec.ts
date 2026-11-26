import { expect, login, logout, press, setAssistant, test } from './helpers'

test('F48 — modèle d\'analyse indisponible : bandeau, formulaire intact', async ({ page }) => {
  await login(page, 'admin')
  await page.goto('/admin/connecteurs')
  const card = page.getByTestId('assistant-admin')
  await card.getByTestId('analysis-disabled').check()
  await press(card.getByTestId('assistant-save'))
  await expect(page.getByTestId('assistant-routing')).toContainText('analyse indisponible')
  await logout(page)

  await login(page, 'alice')
  await press(page.getByRole('link', { name: 'Nouvelle demande', exact: true }))
  // Le formulaire de besoin reste présent même si l'analyse est coupée.
  await expect(page.getByTestId('assistant')).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Décrire le besoin' })).toBeVisible()
  await logout(page)

  // Remettre l'analyse disponible pour les autres specs.
  await login(page, 'admin')
  await page.goto('/admin/connecteurs')
  const card2 = page.getByTestId('assistant-admin')
  await card2.getByTestId('analysis-disabled').uncheck()
  await press(card2.getByTestId('assistant-save'))
  await logout(page)
})

test('F50 — préférences de projet : référent publie, opératrice refuse', async ({ page }) => {
  await login(page, 'alice')
  await page.goto('/projets/vitrine')
  await expect(page.getByTestId('project-preferences')).toBeVisible()
  await page.getByTestId('pref-reason').fill('essai opératrice')
  await press(page.getByTestId('pref-publish'))
  await expect(page.getByTestId('error')).toBeVisible()
  await logout(page)

  await login(page, 'bruno')
  await page.goto('/projets/vitrine')
  await page.getByTestId('pref-tier').selectOption('standard')
  await page.getByTestId('pref-naming').fill('{project_code}-dev-app-{nn}')
  await page.getByTestId('pref-reason').fill('convention vitrine e2e')
  await press(page.getByTestId('pref-publish'))
  await expect(page.getByTestId('preference-current')).toContainText('standard')
  await logout(page)

  // Remettre l'assistant nominal pour les autres specs.
  await setAssistant('fake', 'nominal')
})
