import {
  compose, expect, login, planViaApi, press, session, test, waitState,
} from './helpers'

// F37 : plan approuvé en file (worker arrêté) ; ouverture du disjoncteur via l'API admin ;
// au redémarrage, tf_apply bloqué connector_circuit_open ; inventaire (read) admis.
// Isolation : le disjoncteur est refermé en beforeAll / finally / afterAll (sinon D5 et atelier
// restent bloqués sur connector_circuit_open).

const REASON = 'vCenter de test injoignable (démo F37)'
let planned: any

type ApiSession = Awaited<ReturnType<typeof session>>

async function vsphere(admin: ApiSession): Promise<{ id: string, circuit: { state: string } }> {
  const connectors = (await admin.call('GET', '/connectors')).body as any[]
  const fake = connectors.find((c) => c.slug === 'vsphere-fake')
  expect(fake, 'connecteur vsphere-fake').toBeTruthy()
  return fake
}

/** Referme le disjoncteur via le test de santé simulé (obligatoire pour l'isolation e2e). */
async function ensureCircuitClosed(admin: ApiSession): Promise<void> {
  const fake = await vsphere(admin)
  if (fake.circuit?.state !== 'open') return
  const health = await admin.call('POST', `/connectors/${fake.id}/test`)
  expect(health.status, JSON.stringify(health.body)).toBe(200)
  const after = await vsphere(admin)
  expect(after.circuit.state, JSON.stringify(health.body)).toBe('closed')
}

test.beforeAll(async () => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  const admin = await session('admin')
  try {
    await ensureCircuitClosed(admin)
    planned = await planViaApi(alice, 'vitrine', 'F37 : approuvée avant le disjoncteur')
  } finally {
    await alice.dispose()
    await admin.dispose()
  }
})

test.afterAll(async () => {
  const admin = await session('admin')
  try {
    await ensureCircuitClosed(admin)
  } finally {
    await admin.dispose()
  }
})

test('F37 — disjoncteur : write bloqué, read inventaire admis', async ({ page }) => {
  // Timeout large : arrêt/redémarrage worker + admission ; ne pas croiser waitState(180s).
  test.setTimeout(300_000)
  const alice = await session('alice')
  const admin = await session('admin')
  const fake = await vsphere(admin)
  try {
    compose('stop', 'worker')
    try {
      await login(page, 'bruno')
      await page.goto(`/demandes/${planned.id}`)
      await expect(page.getByTestId('plan-panel')).toBeVisible({ timeout: 30_000 })
      await press(page.getByTestId('approve'))
      await expect(page.getByText('Plan approuvé : exécution du plan approuvé.')).toBeVisible({ timeout: 30_000 })
      const applyQueued = page.getByTestId('job').filter({ hasText: 'Application du plan' })
      await expect(applyQueued).toContainText('En file', { timeout: 30_000 })

      const open = await admin.call('POST', `/connectors/${fake.id}/circuit/open`, { reason: REASON })
      expect(open.status).toBe(200)
      expect(open.body.connector.circuit.state).toBe('open')
      expect(open.body.write_jobs.queued).toBeGreaterThanOrEqual(1)

      // Comme F27 : garantir la réclamation dès le redémarrage (fenêtre / fuseau CI).
      compose(
        'exec', '-T', 'postgres',
        'psql', '-U', 'rackloom_owner', '-d', 'rackloom', '-v', 'ON_ERROR_STOP=1', '-c',
        `UPDATE jobs SET run_after = now() - interval '1 second' WHERE change_id = '${planned.id}' AND kind = 'tf_apply' AND state = 'queued';`,
      )
    } finally {
      // --wait : le worker est prêt avant le poll d'admission (évite 180s sur « Approuvée »).
      compose('up', '-d', '--wait', 'worker')
    }

    const stillOpen = await vsphere(admin)
    expect(stillOpen.circuit.state, 'disjoncteur encore ouvert après redémarrage worker').toBe('open')

    // Preuve serveur d'abord (indépendante du SSE UI), puis badge.
    await expect.poll(async () => {
      const jobs = (await alice.call('GET', `/changes/${planned.id}/jobs`)).body
      const job = jobs.find((j: any) => j.kind === 'tf_apply')
      return job ? [job.state, job.blocked_reason, job.dispatched_at] : null
    }, { timeout: 120_000, intervals: [1000] }).toEqual(['blocked', 'connector_circuit_open', null])

    await page.goto(`/demandes/${planned.id}`)
    await waitState(page, "Bloquée à l'admission", 60_000)
    const apply = page.getByTestId('job').filter({ hasText: 'Application du plan' })
    await expect(apply).toContainText('Bloqué', { timeout: 30_000 })
    await expect(page.getByTestId('timeline')).toContainText('R095')

    // Lecture toujours admise pendant le disjoncteur ouvert.
    const inv = await admin.call('POST', `/connectors/${fake.id}/inventory-refresh`)
    expect([inv.status, inv.body.simulated]).toEqual([200, true])
  } finally {
    try {
      await ensureCircuitClosed(admin)
    } catch (e) {
      console.error('F37 teardown : fermeture du disjoncteur', e)
    }
    await alice.dispose()
    await admin.dispose()
  }
})
