import { expect, expectAccessible, login, logout, session, test } from './helpers'

// F23 : panneau télémétrie projet — quotas visibles ; liste vide sans agent.
test('F23 — passerelle de télémétrie (lecture projet)', async ({ page }) => {
  test.setTimeout(120_000)
  const alice = await session('alice')
  try {
    const report = await alice.call('GET', '/projects/vitrine/telemetry')
    expect(report.status).toBe(200)
    expect(report.body.limits.max_payload_bytes).toBeGreaterThan(0)
    expect(report.body.quota.limit_bytes).toBeGreaterThan(0)
    expect(Array.isArray(report.body.samples)).toBe(true)

    const missing = await alice.call('GET', '/projects/compta/telemetry')
    expect(missing.status).toBe(404)

    await login(page, 'alice')
    await page.goto('/projets/vitrine')
    const panel = page.getByTestId('telemetry-panel')
    await expect(panel).toBeVisible()
    await expect(page.getByTestId('telemetry-quota')).toBeVisible()
    await expectAccessible(page, 'F23 télémétrie')
    await logout(page)
  } finally {
    await alice.dispose()
  }
})
