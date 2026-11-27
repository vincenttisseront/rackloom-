import { expect, expectAccessible, api, login, test } from './helpers'

// F41–F45 : page Identités (SCIM, revue, export d'audit, alertes TLS).

test('F41–F45 — identités et audit : SCIM, revue, export', async ({ page }) => {
  test.setTimeout(180_000)
  await login(page, 'admin')

  // Semer une anomalie F42 : compte désactivé qui conserve un rôle (comme les tests d'intégration).
  const envs = (await api(page, 'GET', '/admin/environments')).body as any[]
  const vit = envs.find((e: any) => e.project?.slug === 'vitrine') || envs[0]
  expect(vit?.project?.id, 'projet vitrine').toBeTruthy()
  const ghost = await api(page, 'POST', '/users', {
    email: `ghost-e2e-${Date.now()}@demo.invalid`,
    display_name: 'Ghost E2E',
    password: 'mot-de-passe-de-test-xxxx',
  })
  expect(ghost.status, JSON.stringify(ghost.body)).toBe(200)
  const grant = await api(page, 'POST', '/role-bindings', {
    user_id: ghost.body.id,
    role: 'operator',
    project_id: vit.project.id,
  })
  expect(grant.status, JSON.stringify(grant.body)).toBe(200)
  const minted = await api(page, 'POST', '/admin/scim/tokens', { label: 'e2e-annuaire' })
  expect(minted.status, JSON.stringify(minted.body)).toBe(200)
  const origin = new URL(page.url()).origin
  // SCIM PATCH : Bearer, pas de CSRF cookie.
  const patch = await page.request.fetch(`${origin}/api/v1/scim/v2/Users/${ghost.body.id}`, {
    method: 'PATCH',
    headers: {
      Authorization: `Bearer ${minted.body.token}`,
      'Content-Type': 'application/scim+json',
      Origin: origin,
    },
    data: {
      schemas: ['urn:ietf:params:scim:api:messages:2.0:PatchOp'],
      Operations: [{ op: 'Replace', path: 'active', value: false }],
    },
  })
  expect(patch.status(), await patch.text()).toBe(200)

  // Hub /admin (évite le 404 Nuxt sur la section « Administration »).
  await page.goto('/admin')
  await expect(page.getByRole('heading', { level: 1, name: /^Administration$/ })).toBeVisible()
  await expect(page.getByTestId('admin-hub')).toBeVisible()
  await page.getByTestId('admin-link-identites').click()
  await expect(page.getByRole('heading', { level: 1, name: /Identités et audit/ })).toBeVisible()
  await expect(page.getByTestId('scim-panel')).toBeVisible()
  await expect(page.getByTestId('review-panel')).toBeVisible()
  await expect(page.getByTestId('export-panel')).toBeVisible()
  await expect(page.getByTestId('tls-panel')).toBeVisible()
  await expectAccessible(page, 'F41–F45 identités')

  await page.getByTestId('scim-mint').click()
  await expect(page.getByTestId('scim-token')).toBeVisible()

  await page.getByTestId('review-prepare').click()
  await expect(page.getByTestId('notice')).toContainText(/Campagne préparée/)
  await expect(page.getByTestId('review-items')).toBeVisible()
  await expect(page.getByTestId('review-items')).toContainText(/unjustified_binding|Ghost|désactivé/)

  await page.getByTestId('export-create').click()
  await expect(page.getByTestId('error')).toHaveCount(0)
  const run = page.locator('[data-testid^="export-run-"]').first()
  await expect(run).toBeVisible()
  await expect(page.getByTestId('notice')).toContainText(/Destination/)
  await run.click()
  await expect(page.getByTestId('notice')).toContainText(/Export/)
})
