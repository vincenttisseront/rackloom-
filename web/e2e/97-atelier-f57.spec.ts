import {
  approveAsBruno, expect, expectAccessible, inDays, login, press, session, test, waitState,
} from './helpers'

test('F57 — fiche Votre service est prêt (quatre états)', async ({ page }) => {
  test.setTimeout(600_000)
  const alice = await session('alice')

  // Déploiement réel → état prêt.
  await login(page, 'alice')
  await page.goto('/projets/vitrine/nouvelle')
  await press(page.locator('#svc-ubuntu-web'), 'Space')
  await page.locator('#f-quantity').fill('1')
  await page.getByLabel(/Date de fin/).fill(inDays(14))
  await press(page.getByTestId('continue'))
  await press(page.getByTestId('submit'))
  await waitState(page, 'Plan prêt')
  const ref = (await page.locator('.crumbs').innerText()).split('›').pop()!.trim()
  await approveAsBruno(page, ref)
  await expect(page.getByTestId('service-result')).toContainText('Votre service est prêt', { timeout: 300_000 })
  await expect(page.getByTestId('service-headline')).toHaveText('Votre service est prêt')
  await expect(page.getByTestId('service-checks-ok')).toBeVisible()
  await expect(page.getByTestId('service-access')).toContainText(/HTTP/)
  await expectAccessible(page, 'F57 prêt')

  // UI des trois autres états via interception du GET déploiement (la page embarque service_card).
  // Le calcul serveur des états est couvert par T41 + test_f57_service_card.
  const samples: { status: string; headline: string; detail: string }[] = [
    {
      status: 'created_not_ready',
      headline: 'Service créé, mais pas prêt',
      detail: 'Créé mais pas prêt : sonde HTTP en échec',
    },
    { status: 'verification_unavailable', headline: 'Vérification indisponible', detail: 'Vérification indisponible' },
    { status: 'failed', headline: 'Échec du service', detail: "aucune ressource attendue n'a été créée" },
  ]
  const cardFor = (sample: (typeof samples)[number]) => ({
    deployment_id: '00000000-0000-0000-0000-000000000057',
    status: sample.status,
    status_label: sample.status,
    headline: sample.headline,
    detail: sample.detail,
    service: { title: 'Serveur web Ubuntu', slug: 'ubuntu-web', version: '1.0.0' },
    environment: { slug: 'dev', name: 'dev', is_production: false },
    project: { slug: 'vitrine', name: 'Vitrine' },
    owner: 'Alice (opératrice)',
    access_mode: 'HTTP (port 80)',
    outputs: [{ key: 'name', label: 'Nom', value: 'vit-dev-web-01' }],
    checked_at: new Date().toISOString(),
    checks: {
      succeeded: [],
      failed: sample.status === 'created_not_ready'
        ? [{ host: 'vit-dev-web-01', type: 'http_probe', label: 'Sonde HTTP', failure: 'sonde HTTP en échec', evidence: '', checked_at: null }]
        : [],
      summary: '0 réussie(s), 1 en échec',
    },
    technical: { deployment_status: 'active', service_reason: sample.detail, hosts: [], note: 'test' },
    notice: "L'état du service vient des contrôles de santé observés, pas du seul succès d'un job.",
  })
  for (const sample of samples) {
    await page.route('**/api/v1/deployments/*', async (route) => {
      const url = route.request().url()
      // Ne pas intercepter service-card / changes : la page lit GET /deployments/{id}.
      if (url.includes('/service-card') || url.includes('/changes')) {
        await route.continue()
        return
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          id: '00000000-0000-0000-0000-000000000057',
          status: 'active',
          service_status: sample.status,
          service_card: cardFor(sample),
        }),
      })
    })
    await page.goto('/deployments/00000000-0000-0000-0000-000000000057')
    await expect(page.getByTestId('service-headline')).toHaveText(sample.headline)
    await expect(page.getByTestId('service-detail')).toContainText(sample.detail.split(':').pop()!.trim().slice(0, 12))
    await expect(page.getByTestId('service-card')).toHaveAttribute('data-status', sample.status)
    await page.unroute('**/api/v1/deployments/*')
  }

  // Hors périmètre → 404 (pas de fuite).
  const chloe = await session('chloe')
  const deps = (await alice.call('GET', '/projects/vitrine/deployments')).body as { id: string }[]
  expect(deps.length).toBeGreaterThan(0)
  const foreign = await chloe.call('GET', `/deployments/${deps[0].id}/service-card`)
  expect(foreign.status).toBe(404)
  await chloe.dispose()
  await alice.dispose()
})
