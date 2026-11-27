import { expect, expectAccessible, login, press, session, test } from './helpers'

test('F58/F59 — actions fiche service et demande d\'accès', async ({ page }) => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  const bruno = await session('bruno')
  const chloe = await session('chloe')

  const deps = (await alice.call('GET', '/projects/vitrine/deployments')).body as { id: string }[]
  expect(deps.length, 'au moins un déploiement vitrine (parcours antérieur)').toBeGreaterThan(0)
  const deploymentId = deps[0].id

  // F59 API : champ group → 422 locked_field.
  const bad = await alice.call('POST', `/deployments/${deploymentId}/access-requests`, {
    access_level: 'read',
    group: 'admins',
  })
  expect(bad.status).toBe(422)
  expect(bad.body.code).toBe('locked_field')

  // F58 API : hors périmètre → 404.
  const foreign = await chloe.call('POST', `/deployments/${deploymentId}/actions/increase_capacity`, {})
  expect(foreign.status).toBe(404)

  // Fiche : actions et panneau d'accès.
  await login(page, 'alice')
  await page.goto(`/deployments/${deploymentId}`)
  await expect(page.getByTestId('service-card')).toBeVisible({ timeout: 30_000 })

  const actions = page.getByTestId('service-actions')
  // Actions présentes si le service est prêt/actif ; sinon panneau d'accès seul.
  if (await actions.count()) {
    await expect(page.getByTestId('service-action-impact').first()).toBeVisible()
    await expect(page.getByTestId('service-action-increase_capacity').or(
      page.getByTestId('service-action-request_restart'),
    )).toBeVisible()
  }
  await expect(page.getByTestId('access-request-panel')).toBeVisible()
  await expectAccessible(page, 'F58/F59 fiche')

  // Demande d'accès (si niveaux éligibles).
  if (await page.getByTestId('request-access').count()) {
    await press(page.getByTestId('request-access'))
    await expect(page.getByTestId('notice')).toContainText(/accès/i, { timeout: 20_000 })

    await login(page, 'bruno')
    await page.goto(`/deployments/${deploymentId}`)
    const approve = page.getByTestId('approve-access')
    await expect(approve).toBeVisible({ timeout: 20_000 })
    await press(approve)
    await expect(page.getByTestId('notice')).toContainText(/accordé|synchronisation|Échec/i, {
      timeout: 20_000,
    })
  }

  // F58 : augmenter la capacité → demande préremplie (si action dispo).
  await login(page, 'alice')
  await page.goto(`/deployments/${deploymentId}`)
  const cap = page.getByTestId('service-action-increase_capacity')
  if (await cap.count()) {
    await press(cap)
    await expect(page.getByRole('heading', { name: /Modifier ce déploiement|Demande|Récapitulatif/i })).toBeVisible({
      timeout: 30_000,
    })
    // Impact remplacement visible sur l'étape 2 si présent.
    const risk = page.getByTestId('risk-destructive')
    if (await risk.count()) {
      await expect(risk).toContainText(/remplac/i)
    }
  }

  await alice.dispose()
  await bruno.dispose()
  await chloe.dispose()
})
