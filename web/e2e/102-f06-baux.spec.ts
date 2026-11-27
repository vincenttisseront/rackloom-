import { expect, expectAccessible, inDays, login, logout, press, session, test } from './helpers'

test('F06 — baux temporaires : renouvellement et signalement', async ({ page }) => {
  test.setTimeout(180_000)
  const alice = await session('alice')
  const chloe = await session('chloe')

  // Créer une demande avec date de fin → bail ouvert à la soumission.
  const created = await alice.call('POST', '/projects/vitrine/changes', {
    title: 'Labo F06 e2e',
  })
  expect(created.status, JSON.stringify(created.body)).toBe(200)
  const changeId = created.body.id as string
  const versioned = await alice.call('POST', `/changes/${changeId}/versions`, {
    base_version_no: 0,
    source: 'api',
    need: {
      schema: 'rackloom.need/v1',
      service: { template: 'ubuntu-web' },
      environment: 'dev',
      confirm_production: false,
      inputs: {
        quantity: 1,
        size_tier: 'test_leger',
        usage: 'Bail e2e F06',
        lifetime_until: inDays(10),
      },
    },
  })
  expect(versioned.status, JSON.stringify(versioned.body)).toBe(200)
  const revisionNo = versioned.body.revision_no as number
  const submitted = await alice.call('POST', `/changes/${changeId}/submit`, {
    revision_no: revisionNo,
  })
  expect(submitted.status, JSON.stringify(submitted.body)).toBe(200)
  const deploymentId = submitted.body.deployment_id as string
  expect(deploymentId).toBeTruthy()

  const lease = await alice.call('GET', `/deployments/${deploymentId}/lease`)
  expect(lease.status, JSON.stringify(lease.body)).toBe(200)
  expect(lease.body.state).toBe('active')
  expect(lease.body.expires_on).toBe(inDays(10))
  const beforeExpires = lease.body.expires_on as string

  // API : prolonger explicitement (plafond absolu depuis aujourd'hui).
  const renewed = await alice.call('POST', `/deployments/${deploymentId}/lease/renew`, {
    days: 14,
    reason: 'Renouvellement e2e F06',
  })
  expect(renewed.status, JSON.stringify(renewed.body)).toBe(200)
  expect(renewed.body.state).toBe('active')
  expect(renewed.body.expires_on).toBe(inDays(14))

  const foreign = await chloe.call('GET', `/deployments/${deploymentId}/lease`)
  expect(foreign.status).toBe(404)

  await login(page, 'alice')
  await page.goto(`/deployments/${deploymentId}`)
  await expect(page.getByTestId('service-card')).toBeVisible({ timeout: 30_000 })
  await expect(page.getByTestId('lease-panel')).toBeVisible()
  await expect(page.getByTestId('lease-renew')).toBeVisible()
  // UI : prolonge depuis expires_on courant (+7 j.) — ne doit pas renvoyer not_extended.
  await press(page.getByTestId('lease-renew'))
  await expect(page.getByTestId('notice')).toContainText(/renouvelé/i, { timeout: 20_000 })
  await expect(page.getByTestId('error')).toHaveCount(0)

  const after = await alice.call('GET', `/deployments/${deploymentId}/lease`)
  expect(after.status).toBe(200)
  expect(after.body.expires_on > beforeExpires).toBe(true)
  // UI ajoute 7 jours à la date courante du bail (après renew API à J+14 → J+21).
  expect(after.body.expires_on).toBe(inDays(21))

  await expectAccessible(page, 'F06 bail fiche')

  await logout(page)
  await alice.dispose()
  await chloe.dispose()
})
