import {
  expect, expectAccessible, login, logout, planViaApi, press, session, test, waitState, type Page,
} from './helpers'

// F21 : chronologie unifiée — sources ADR, reprise après coupure navigateur, cloisonnement, sans doublon.

let planned: any

test.beforeAll(async () => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  try {
    planned = await planViaApi(alice, 'vitrine', 'F21 : chronologie unifiée')
  } finally {
    await alice.dispose()
  }
})

const beat = (page: Page) => page.waitForTimeout(700)

test('F21 — chronologie corrélée, reprise sans doublon, cloisonnement', async ({ page }) => {
  test.setTimeout(600_000)
  const alice = await session('alice')
  const chloe = await session('chloe')
  try {
    await login(page, 'alice')
    await page.goto(`/demandes/${planned.id}`)
    await expect(page.getByTestId('timeline')).toBeVisible()
    await expect(page.getByTestId('timeline-item').first()).toBeVisible()

    const sources = page.getByTestId('timeline-source')
    await expect(sources.filter({ hasText: /demande/i }).first()).toBeVisible()
    await expect(sources.filter({ hasText: /Terraform|job/i }).first()).toBeVisible()

    const beforeIds = await page.getByTestId('timeline-item').evaluateAll(
      nodes => nodes.map(n => n.getAttribute('data-event-id')),
    )
    expect(new Set(beforeIds).size).toBe(beforeIds.length)
    expect(beforeIds.length).toBeGreaterThan(1)
    await beat(page)

    // Coupure navigateur : quitter la page (ferme le SSE), puis revenir.
    await page.goto('/')
    await expect(page.getByRole('heading', { level: 1, name: /Bonjour/ })).toBeVisible()
    await page.goto(`/demandes/${planned.id}`)
    await expect(page.getByTestId('timeline-item').first()).toBeVisible()
    const afterIds = await page.getByTestId('timeline-item').evaluateAll(
      nodes => nodes.map(n => n.getAttribute('data-event-id')),
    )
    expect(new Set(afterIds).size).toBe(afterIds.length)
    expect(afterIds.length).toBeGreaterThanOrEqual(beforeIds.length)
    for (const id of beforeIds) expect(afterIds).toContain(id)

    await expectAccessible(page, 'F21 chronologie après reprise')

    let r = await chloe.call('GET', `/changes/${planned.id}/events`)
    expect(r.status).toBe(404)
    r = await chloe.call('GET', `/changes/${planned.id}/events/stream`)
    expect(r.status).toBe(404)

    const events = (await alice.call('GET', `/changes/${planned.id}/events`)).body as any[]
    expect(events.every(e => e.correlation_id === planned.id)).toBe(true)
    const flat = JSON.stringify(events)
    expect(flat).not.toMatch(/"stdout"|"stderr"|"plan_json"|"tfstate"/)

    await logout(page)
    await login(page, 'bruno')
    await page.goto(`/demandes/${planned.id}`)
    await expect(page.getByTestId('plan-panel')).toBeVisible()
    await press(page.getByTestId('approve'))
    await expect(page.getByText('Plan approuvé : exécution du plan approuvé.')).toBeVisible()
    await expect(page.getByTestId('timeline-source').filter({ hasText: /approbation/i }).first()).toBeVisible({
      timeout: 30_000,
    })
    await waitState(page, /Succès|Exécution|Vérification|Approuvée/, 180_000)
    await expect(page.getByTestId('timeline-source').filter({ hasText: /Terraform|vCenter|Ansible|vérification/i }).first())
      .toBeVisible({ timeout: 180_000 })
    await expectAccessible(page, 'F21 chronologie après approbation')
    await beat(page)
  } finally {
    await alice.dispose()
    await chloe.dispose()
  }
})
