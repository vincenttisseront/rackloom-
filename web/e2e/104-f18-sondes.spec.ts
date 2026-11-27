import { expect, expectAccessible, login, logout, session, test } from './helpers'

// F18 : sonde depuis segment filtrant → indisponible ; dimensions distinctes.
// Ne pas prendre le dernier déploiement : F06 (et d'autres) peuvent en créer sans hôtes.
test('F18 — sondes et objectifs de disponibilité', async ({ page }) => {
  test.setTimeout(180_000)
  const alice = await session('alice')
  try {
    const deps = await alice.call('GET', '/projects/vitrine/deployments')
    expect(deps.status).toBe(200)
    expect(deps.body.length).toBeGreaterThan(0)

    let depId: string | null = null
    for (const d of deps.body as { id: string; service_status?: string }[]) {
      const detail = await alice.call('GET', `/deployments/${d.id}`)
      if (detail.status !== 200) continue
      const hosts = (detail.body.hosts || detail.body.service_card?.technical?.hosts || []) as unknown[]
      if (hosts.length > 0) {
        depId = d.id
        break
      }
    }
    expect(depId, 'au moins un déploiement vitrine avec hôtes (parcours D1)').toBeTruthy()

    const run = await alice.call('POST', `/deployments/${depId}/probes/run`, {
      segment: 'filtered',
      service_state: 'active',
      force_probe_ok: true,
    })
    expect(run.status, JSON.stringify(run.body)).toBe(200)
    expect(run.body.service_status).toBe('unavailable')
    expect(run.body.hosts[0].service_state).toBe('active')
    expect(run.body.hosts[0].probe_health).toBe('failed')

    const report = await alice.call('GET', `/deployments/${depId}/availability`)
    expect(report.status).toBe(200)
    expect(report.body.dimensions.service_state).toBe('active')
    expect(report.body.dimensions.probe_health).toBe('failed')
    expect(report.body.dimensions.freshness).toBe('fresh')
    expect(report.body.dimensions.availability).toBe('unavailable')

    const card = await alice.call('GET', `/deployments/${depId}/service-card`)
    expect(card.status).toBe(200)
    expect(card.body.status).toBe('unavailable')
    expect(card.body.availability.service_state).toBe('active')

    // Restaure une sonde OK depuis lab-access pour ne pas laisser le parcours en unavailable.
    const restore = await alice.call('POST', `/deployments/${depId}/probes/run`, {
      segment: 'lab-access',
      service_state: 'active',
      force_probe_ok: true,
    })
    expect(restore.status, JSON.stringify(restore.body)).toBe(200)
    expect(restore.body.service_status).toBe('ready')

    await login(page, 'alice')
    await page.goto(`/deployments/${depId}`)
    await expect(page.getByTestId('service-card')).toBeVisible({ timeout: 30_000 })
    await expect(page.getByTestId('availability-service-state')).toBeVisible()
    await expect(page.getByTestId('availability-probe-health')).toBeVisible()
    await expect(page.getByTestId('availability-freshness')).toBeVisible()
    await expectAccessible(page, 'F18 sondes')
    await logout(page)
  } finally {
    await alice.dispose()
  }
})
