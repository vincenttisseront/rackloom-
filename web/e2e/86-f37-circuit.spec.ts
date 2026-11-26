import {
  compose, expect, expectAccessible, login, planViaApi, press, session, test, waitState, type Page,
} from './helpers'

// F37 : plan approuvé en file (worker arrêté) ; admin ouvre le disjoncteur vSphere ;
// au redémarrage, tf_apply bloqué connector_circuit_open ; inventaire (read) toujours possible.

const REASON = 'vCenter de test injoignable (démo F37)'
let planned: any

test.beforeAll(async () => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  try {
    planned = await planViaApi(alice, 'vitrine', 'F37 : approuvée avant le disjoncteur')
  } finally {
    await alice.dispose()
  }
})

const beat = (page: Page) => page.waitForTimeout(700)

test('F37 — disjoncteur : write bloqué, read inventaire admis', async ({ page }) => {
  test.setTimeout(600_000)
  const alice = await session('alice')
  const admin = await session('admin')
  try {
    compose('stop', 'worker')
    try {
      await login(page, 'bruno')
      await page.goto(`/demandes/${planned.id}`)
      await expect(page.getByTestId('plan-panel')).toBeVisible()
      await press(page.getByTestId('approve'))
      await expect(page.getByText('Plan approuvé : exécution du plan approuvé.')).toBeVisible()
      const apply = page.getByTestId('job').filter({ hasText: 'Application du plan' })
      await expect(apply).toContainText('En file')
      await beat(page)

      const connectors = (await admin.call('GET', '/connectors')).body
      const fake = connectors.find((c: any) => c.slug === 'vsphere-fake')
      const open = await admin.call('POST', `/connectors/${fake.id}/circuit/open`, { reason: REASON })
      expect(open.status).toBe(200)
      expect(open.body.connector.circuit.state).toBe('open')
      expect(open.body.write_jobs.queued).toBeGreaterThanOrEqual(1)

      await login(page, 'admin')
      await page.goto('/admin/connecteurs')
      await expect(page.getByTestId('connector-vsphere-fake').getByTestId('circuit-open')).toBeVisible()
      await expectAccessible(page, 'F37 connecteurs disjoncteur ouvert')
    } finally {
      compose('start', 'worker')
    }

    await page.goto(`/demandes/${planned.id}`)
    await waitState(page, "Bloquée à l'admission")
    const apply = page.getByTestId('job').filter({ hasText: 'Application du plan' })
    await expect(apply).toContainText('Bloqué')
    const jobs = (await alice.call('GET', `/changes/${planned.id}/jobs`)).body
    const job = jobs.find((j: any) => j.kind === 'tf_apply')
    expect([job.state, job.blocked_reason, job.dispatched_at]).toEqual(['blocked', 'connector_circuit_open', null])
    await expect(page.getByTestId('timeline')).toContainText('R095')

    const connectors = (await admin.call('GET', '/connectors')).body
    const fake = connectors.find((c: any) => c.slug === 'vsphere-fake')
    const inv = await admin.call('POST', `/connectors/${fake.id}/inventory-refresh`)
    expect([inv.status, inv.body.simulated]).toEqual([200, true])

    // Santé simulée OK ferme le disjoncteur (pas d'écriture incertaine).
    const health = await admin.call('POST', `/connectors/${fake.id}/test`)
    expect(health.status).toBe(200)
    expect(health.body.circuit.state).toBe('closed')
    expect(health.body.circuit_closed).toBe(true)
  } finally {
    await alice.dispose()
    await admin.dispose()
  }
})
