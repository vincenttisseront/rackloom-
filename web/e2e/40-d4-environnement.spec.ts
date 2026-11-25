import {
  D1, api, describeByForm, expect, expectAccessible, grants, login, need, press, setAssistant, test, typeInto,
} from './helpers'

// D4 (T34) : sans environnement, dev et jamais prod ; réseau de dev retiré → blocage, jamais prod ;
// production seulement après confirmation explicite, avec son circuit d'approbation.
test('D4 — dev par défaut, jamais de bascule vers la production (T34)', async ({ page }) => {
  test.setTimeout(240_000)
  await login(page, 'alice')
  await page.goto('/projets/vitrine/nouvelle')
  await describeByForm(page)
  await expect(page.getByTestId('summary-headline')).toContainText('dans Vitrine / dev')
  const cid = new URL(page.url()).searchParams.get('change')!
  let view = (await api(page, 'GET', `/changes/${cid}`)).body
  expect(view.revision.environment.slug).toBe('dev')
  expect(view.need.environment).toBeNull()

  // Octroi du réseau de dev retiré : blocage expliqué, même environnement.
  grants('revoke', 'vitrine', 'dev', 'network')
  try {
    await press(page.getByRole('button', { name: 'Modifier le besoin' }))
    await press(page.getByTestId('continue'))
    await expect(page.getByTestId('block')).toHaveText(
      "Aucun réseau n'est disponible pour ce profil. Contactez le référent du projet Vitrine (Bruno).")
    await expect(page.getByTestId('submit')).toHaveCount(0)
    view = (await api(page, 'GET', `/changes/${cid}`)).body
    expect([view.revision.outcome, view.revision.environment.slug]).toEqual(['blocked', 'dev'])
  } finally {
    grants('restore', 'vitrine', 'dev', 'network')
  }

  // API : la production sans confirmation est refusée, aucune version créée.
  const before = view.current_version_no
  const r = await api(page, 'POST', `/changes/${cid}/versions`,
    { need: need({ environment: 'prod' }), base_version_no: before })
  expect([r.status, r.body?.code]).toEqual([422, 'production_not_confirmed'])

  // Chat : le scénario defaults_prod (production proposée par défaut) est rejeté.
  await setAssistant('fake', 'defaults_prod')
  try {
    await press(page.getByRole('button', { name: 'Modifier le besoin' }))
    await typeInto(page.getByLabel("Message pour l'assistant"), D1)
    await page.keyboard.press('Enter')
    await expect(page.locator('.bubble.bot').last()).toContainText("Suggestion de l'assistant rejetée")
  } finally {
    await setAssistant('fake', 'nominal')
  }
  expect((await api(page, 'GET', `/changes/${cid}`)).body.current_version_no).toBe(before)

  // Production demandée explicitement : bandeau, puis circuit de la production à l'étape 2.
  await press(page.getByTestId('ask-production'))
  await expect(page.getByRole('dialog', { name: 'Demander un déploiement en production ?' })).toBeVisible()
  await press(page.getByTestId('confirm-production'))
  await expect(page.getByTestId('production-banner')).toContainText('approbateur distinct obligatoire')
  await expectAccessible(page, 'D4 production demandée')
  await press(page.getByTestId('continue'))
  await expect(page.getByTestId('summary-headline')).toContainText('Vitrine / prod')
  await expect(page.getByTestId('review')).toContainText("circuit d'approbation de la production")
  view = (await api(page, 'GET', `/changes/${cid}`)).body
  expect([view.revision.outcome, view.revision.environment.slug, view.need.confirm_production])
    .toEqual(['resolved', 'prod', true])
  expect(view.revision.summary.approval).toMatch(/autre que vous|distinct/)
})
