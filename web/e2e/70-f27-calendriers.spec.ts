import {
  compose, expect, expectAccessible, login, logout, planViaApi, press, session, test, waitState, type Page,
} from './helpers'

// F27 : plan approuvé en file (worker arrêté) ; gel calendaire planifié ; au redémarrage le job
// d'écriture est bloqué (calendar_frozen / R090) ; lecture et diagnostic restent disponibles.
// Gel posé via l'API avec horodatages UTC explicites (évite datetime-local / fuseau du runner CI).

const REASON = 'Gel planifié F27 : maintenance réseau de démonstration'
let planned: any

test.beforeAll(async () => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  try {
    planned = await planViaApi(alice, 'vitrine', 'F27 : approuvée avant le gel calendaire')
  } finally {
    await alice.dispose()
  }
})

const beat = (page: Page) => page.waitForTimeout(700)

async function openProject(page: Page) {
  const crumb = page.locator('.crumbs').getByRole('link', { name: 'Vitrine' })
  if (await crumb.count()) await press(crumb)
  else await press(page.getByRole('heading', { level: 2 }).getByRole('link', { name: 'Vitrine' }))
  await expect(page.getByRole('heading', { level: 1, name: 'Vitrine' })).toBeVisible()
}

test('F27 — gel calendaire : job en file bloqué à l\'admission, lecture disponible', async ({ page }) => {
  test.setTimeout(600_000)
  const alice = await session('alice')
  const chloe = await session('chloe')
  const bruno = await session('bruno')
  const dev = (await alice.call('GET', '/projects/vitrine/environments')).body.find((e: any) => e.slug === 'dev')
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

      const freeze = await bruno.call('POST', `/environments/${dev.id}/calendar/freezes`, {
        starts_at: new Date(Date.now() - 60_000).toISOString(),
        ends_at: new Date(Date.now() + 3_600_000).toISOString(),
        reason: REASON,
      })
      expect(freeze.status, JSON.stringify(freeze.body)).toBe(200)
      expect(freeze.body.state?.code).toBe('calendar_frozen')

      await openProject(page)
      const panel = page.getByTestId('calendar-dev')
      await expect(panel.getByTestId('calendar-freeze')).toContainText(REASON)
      await expect(page.getByTestId('project-calendar-banner')).toContainText(/gel|période/i)
      await expectAccessible(page, 'F27 calendrier gelé')

      let r = await alice.call('POST', `/environments/${dev.id}/calendar/freezes`, {
        starts_at: new Date().toISOString(),
        ends_at: new Date(Date.now() + 3600_000).toISOString(),
        reason: 'tentative opératrice',
      })
      expect([r.status, r.body?.code]).toEqual([403, 'forbidden'])
      r = await chloe.call('GET', `/environments/${dev.id}/calendar`)
      expect(r.status).toBe(404)

      // Réclamation garantie dès le redémarrage du worker (now() côté PostgreSQL).
      compose(
        'exec', '-T', 'postgres',
        'psql', '-U', 'rackloom_owner', '-d', 'rackloom', '-v', 'ON_ERROR_STOP=1', '-c',
        `UPDATE jobs SET run_after = now() - interval '1 second' WHERE change_id = '${planned.id}' AND kind = 'tf_apply' AND state = 'queued';`,
      )
    } finally {
      // --wait : worker prêt avant le poll d'admission (évite 180s sur « Approuvée », comme F37).
      compose('up', '-d', '--wait', 'worker')
    }

    // Preuve serveur d'abord (indépendante du SSE UI), puis badge.
    await expect.poll(async () => {
      const jobs = (await alice.call('GET', `/changes/${planned.id}/jobs`)).body
      const job = jobs.find((j: any) => j.kind === 'tf_apply')
      return job ? [job.state, job.blocked_reason, job.dispatched_at] : null
    }, { timeout: 120_000, intervals: [1000] }).toEqual(['blocked', 'calendar_frozen', null])

    // Session fraîche après l'attente worker : évite change-state introuvable (flake CI).
    await logout(page)
    await login(page, 'bruno')
    await page.goto(`/demandes/${planned.id}`)
    await expect(page.getByTestId('change-state')).toBeVisible({ timeout: 60_000 })
    await waitState(page, "Bloquée à l'admission", 120_000)
    const apply = page.getByTestId('job').filter({ hasText: 'Application du plan' })
    await expect(apply).toContainText('Bloqué', { timeout: 30_000 })
    await expect(page.getByTestId('timeline')).toContainText(/R090|période de gel|calendrier/i)
    await beat(page)

    const admin = await session('admin')
    try {
      const connectors = (await admin.call('GET', '/connectors')).body
      const fake = connectors.find((c: any) => c.slug === 'vsphere-fake')
      const inv = await admin.call('POST', `/connectors/${fake.id}/inventory-refresh`)
      expect(inv.status, JSON.stringify(inv.body)).toBe(200)
      expect(inv.body.simulated).toBe(true)
    } finally {
      await admin.dispose()
    }

    await logout(page)
    await login(page, 'alice')
    await page.goto(`/demandes/${planned.id}`)
    await press(page.getByRole('button', { name: "Expliquer avec l'assistant" }))
    await expect(page.getByText('Voici les derniers événements de ce job')).toBeVisible()
    await expectAccessible(page, 'F27 demande bloquée par le gel calendaire')
    await beat(page)

    await logout(page)
    await login(page, 'bruno')
    await openProject(page)
    await expect(page.getByTestId('project-calendar-banner')).toBeVisible()
    await press(page.getByTestId('calendar-freeze-cancel'))
    await expect(page.getByTestId('project-calendar-banner')).toHaveCount(0)
    await beat(page)
  } finally {
    const cal = await bruno.call('GET', `/environments/${dev.id}/calendar`)
    for (const f of (cal.body.calendar_freezes || []).filter((x: any) => x.active)) {
      await bruno.call('POST', `/environments/${dev.id}/calendar/freezes/${f.id}/cancel`, {
        reason: 'Fin du scénario F27',
      })
    }
    await alice.dispose()
    await chloe.dispose()
    await bruno.dispose()
  }
})
