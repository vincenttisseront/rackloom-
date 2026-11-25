import {
  compose, expect, expectAccessible, login, logout, planViaApi, press, session, test, typeInto, waitState, type Page,
} from './helpers'

// F28 : un plan approuvé attend en file (worker arrêté) ; l'approbateur gèle l'environnement avec un motif ;
// au redémarrage du worker, le job d'écriture est bloqué au démarrage (env_frozen) sans rien envoyer ;
// la lecture et le diagnostic restent disponibles ; refus côté serveur (403 sans le rôle, 404 hors périmètre).

const FREEZE = 'Incident réseau sur le site : gel des écritures (démo F28)'
const UNFREEZE = 'Incident clos : reprise des écritures'
let planned: any

test.beforeAll(async () => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  try {
    planned = await planViaApi(alice, 'vitrine', 'F28 : approuvée avant le gel')
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

async function toggleFreeze(page: Page, action: 'freeze' | 'unfreeze', reason: string) {
  const dev = page.getByTestId('env-dev')
  await press(dev.getByTestId(`env-${action}`))
  await typeInto(page.getByTestId('freeze-reason'), reason)
  await beat(page)
  await press(page.getByTestId('freeze-confirm'))
  await expect(dev.getByTestId('env-state')).toHaveText(action === 'freeze' ? 'Gelé' : 'Actif')
}

test('F28 — gel d\'urgence : job en file bloqué au démarrage, lecture et diagnostic disponibles', async ({ page }) => {
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

      await openProject(page)
      const row = page.getByTestId('env-dev')
      await expect(row.getByTestId('env-state')).toHaveText('Actif')
      await expect(row).toContainText('1 en attente')
      await toggleFreeze(page, 'freeze', FREEZE)
      await expect(row.getByTestId('env-freeze')).toContainText(FREEZE)
      await expect(row).toContainText('(ne démarreront pas)')
      await expect(page.getByTestId('project-freeze-banner')).toContainText('Environnement dev gelé')
      await expectAccessible(page, 'F28 environnement gelé')
      await beat(page)

      // Masquer un bouton n'est pas un contrôle : le serveur refuse lui-même.
      let r = await alice.call('POST', `/environments/${dev.id}/unfreeze`, { reason: 'tentative opératrice' })
      expect([r.status, r.body?.code]).toEqual([403, 'forbidden'])
      r = await chloe.call('POST', `/environments/${dev.id}/unfreeze`, { reason: 'tentative autre projet' })
      expect(r.status).toBe(404)
      r = await chloe.call('GET', `/environments/${dev.id}`)
      expect(r.status).toBe(404)
    } finally {
      compose('start', 'worker')
    }

    // Le worker réévalue le job au démarrage effectif : bloqué, jamais envoyé au fournisseur.
    await page.goto(`/demandes/${planned.id}`)
    await expect(page.getByTestId('freeze-banner')).toContainText(FREEZE)
    await waitState(page, "Bloquée à l'admission")
    const apply = page.getByTestId('job').filter({ hasText: 'Application du plan' })
    await expect(apply).toContainText('Bloqué')
    await expect(apply.getByTestId('job-blocked-reason')).toContainText('Environnement gelé')
    await expect(page.getByTestId('timeline')).toContainText('Environnement dev gelé par Bruno (approbateur)')
    await expect(page.getByTestId('timeline')).toContainText("bloqué à l'admission — Admission refusée (R080)")
    const jobs = (await alice.call('GET', `/changes/${planned.id}/jobs`)).body
    const job = jobs.find((j: any) => j.kind === 'tf_apply')
    expect([job.state, job.blocked_reason, job.dispatched_at]).toEqual(['blocked', 'env_frozen', null])
    await beat(page)

    // Lecture et diagnostic restent disponibles pendant le gel (rôle opérateur pour l'assistant).
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
    await expect(page.getByTestId('freeze-banner')).toContainText(FREEZE)
    await press(page.getByRole('button', { name: "Expliquer avec l'assistant" }))
    await expect(page.getByText('Voici les derniers événements de ce job')).toBeVisible()
    await expectAccessible(page, 'F28 demande bloquée par le gel')
    await beat(page)

    await logout(page)
    await login(page, 'bruno')
    await openProject(page)
    await toggleFreeze(page, 'unfreeze', UNFREEZE)
    await expect(page.getByTestId('project-freeze-banner')).toHaveCount(0)
    const history = (await alice.call('GET', `/environments/${dev.id}`)).body.history
    expect([history[0].reason, history[0].lift_reason, history[0].lifted_by]).toEqual([FREEZE, UNFREEZE, 'Bruno (approbateur)'])
    await beat(page)
  } finally {
    const current = (await bruno.call('GET', `/environments/${dev.id}`)).body
    if (current?.frozen) await bruno.call('POST', `/environments/${dev.id}/unfreeze`, { reason: 'Nettoyage du test F28' })
    await alice.dispose()
    await chloe.dispose()
    await bruno.dispose()
  }
})
