import {
  compose, expect, expectAccessible, login, logout, planViaApi, press, session, test, typeInto, waitState, type Page,
} from './helpers'

// F27 : plan approuvé en file (worker arrêté) ; gel calendaire planifié ; au redémarrage le job
// d'écriture est bloqué (calendar_frozen / R090) ; lecture et diagnostic restent disponibles.

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
      await expect(page.getByText('Plan approuvé')).toBeVisible()
      const apply = page.getByTestId('job').filter({ hasText: 'Application du plan' })
      await expect(apply).toContainText('En file')
      await beat(page)

      await openProject(page)
      const panel = page.getByTestId('calendar-dev')
      await expect(panel).toBeVisible()
      await typeInto(panel.getByTestId('calendar-freeze-reason'), REASON)
      await beat(page)
      await press(panel.getByTestId('calendar-freeze-add'))
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
    } finally {
      compose('start', 'worker')
    }

    await page.goto(`/demandes/${planned.id}`)
    await waitState(page, /Bloquée/)
    await expect(page.getByTestId('job').filter({ hasText: 'Application du plan' })).toContainText('Bloqué')
    await expect(page.locator('body')).toContainText(/calendrier|période de gel/i)

    await openProject(page)
    await expect(page.getByTestId('project-calendar-banner')).toBeVisible()
    await logout(page)
    await login(page, 'alice')
    await page.goto(`/demandes/${planned.id}`)
    await expect(page.getByTestId('chat-form')).toBeVisible()
  } finally {
    const cal = await bruno.call('GET', `/environments/${dev.id}/calendar`)
    for (const f of (cal.body.calendar_freezes || []).filter((x: any) => x.active || !x.cancelled_at)) {
      if (f.active) {
        await bruno.call('POST', `/environments/${dev.id}/calendar/freezes/${f.id}/cancel`, {
          reason: 'Fin du scénario F27',
        })
      }
    }
    await alice.dispose()
    await chloe.dispose()
    await bruno.dispose()
    compose('start', 'worker')
  }
})
