import { expect, expectAccessible, login, logout, press, session, test } from './helpers'

test('F58/F59 — actions fiche service et demande d\'accès', async ({ page }) => {
  test.setTimeout(180_000)
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
  await expect(page.getByTestId('access-request-panel')).toBeVisible()

  const actions = page.getByTestId('service-actions')
  if (await actions.count()) {
    await expect(page.getByTestId('service-action-impact').first()).toBeVisible()
    await expect(
      page.getByTestId('service-action-increase_capacity').or(page.getByTestId('service-action-request_restart')),
    ).toBeVisible()
  }
  await expectAccessible(page, 'F58/F59 fiche')

  // Demande d'accès via UI (si niveaux éligibles) ; approbation via API Bruno (évite un 2e login UI).
  let requestId: string | null = null
  if (await page.getByTestId('request-access').count()) {
    await press(page.getByTestId('request-access'))
    await expect(page.getByTestId('notice')).toContainText(/accès/i, { timeout: 20_000 })

    const listed = await alice.call('GET', `/deployments/${deploymentId}/access-requests`)
    expect(listed.status).toBe(200)
    const pending = (listed.body.items || []).find((r: any) => r.state === 'pending_approval')
    expect(pending, 'demande en attente').toBeTruthy()
    requestId = pending.id

    const approved = await bruno.call('POST', `/access-requests/${requestId}/approve`, {})
    expect(approved.status, JSON.stringify(approved.body)).toBe(200)
    expect(['granted', 'sync_failed']).toContain(approved.body.state)

    await page.reload()
    await expect(page.getByTestId('access-requests')).toBeVisible({ timeout: 20_000 })
    await expect(page.getByTestId('access-requests')).toContainText(
      /Accès accordé|Échec de synchronisation|accordé|synchronisation/i,
    )
  }

  // F58 : augmenter la capacité → demande préremplie (si action dispo).
  const cap = page.getByTestId('service-action-increase_capacity')
  if (await cap.count()) {
    await press(cap)
    await expect(page.getByRole('heading', { name: /Modifier ce déploiement|Demande|Récapitulatif/i })).toBeVisible({
      timeout: 30_000,
    })
    const risk = page.getByTestId('risk-destructive')
    if (await risk.count()) {
      await expect(risk).toContainText(/remplac/i)
    }
  }

  await logout(page)
  await alice.dispose()
  await bruno.dispose()
  await chloe.dispose()
})
