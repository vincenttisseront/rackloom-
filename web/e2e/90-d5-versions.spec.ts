import {
  approveAsBruno, compose, expect, expectAccessible, login, logout, planViaApi, press, session, test, waitState,
  type Page, type Session,
} from './helpers'

// Exécuté en dernier : il retire ubuntu-web 1.0.0 du registre de la pile de test.

async function templateAction(page: Page, version: string, action: 'publish' | 'withdraw', reason: string) {
  await page.goto('/admin/templates')
  await press(page.getByTestId(`open-ubuntu-web-${version}`))
  await page.getByLabel('Motif (obligatoire)').fill(reason)
  await press(page.getByTestId(`action-${action}`))
  await expect(page.getByTestId('error')).toHaveCount(0)
}

async function versionStatus(s: Session, version: string): Promise<string> {
  const templates = (await s.call('GET', '/admin/templates')).body
  return templates.find((t: any) => t.slug === 'ubuntu-web').versions.find((v: any) => v.version === version).status
}

// D5 (T35) : un plan approuvé en 1.0.0 s'exécute en 1.0.0 même après la publication de 1.1.0 ; une nouvelle
// demande part en 1.1.0 ; le retrait de 1.0.0 avant le démarrage bloque l'exécution (règle 55), plan intact.
test('D5 — version épinglée, publication puis retrait (T35)', async ({ page }) => {
  test.setTimeout(600_000)
  const alice = await session('alice')
  const pub = await session('pub')
  try {
    const a = await planViaApi(alice, 'vitrine', 'D5 : plan A en 1.0.0')
    const b = await planViaApi(alice, 'vitrine', 'D5 : plan B en 1.0.0')
    for (const v of [a, b]) expect(v.plan.bundle.template.version).toBe('1.0.0')

    // Worker arrêté : l'approbation de A et la publication de 1.1.0 précèdent son démarrage.
    compose('stop', 'worker')
    try {
      await login(page, 'alice')
      await approveAsBruno(page, a.ref)
      await logout(page)
      await login(page, 'pub')
      await templateAction(page, '1.1.0', 'publish', 'D5 : nouvelle version de démonstration')
      await expectAccessible(page, 'D5 administration des templates')
      expect(await versionStatus(pub, '1.1.0')).toBe('published')
    } finally {
      compose('start', 'worker')
    }
    await expect.poll(async () => (await alice.call('GET', `/changes/${a.id}`)).body.state,
      { timeout: 300_000, intervals: [2000] }).toBe('succeeded')
    const aDone = (await alice.call('GET', `/changes/${a.id}`)).body
    expect(aDone.plan.bundle_digest).toBe(a.plan.bundle_digest)
    expect(aDone.deployment.template.version).toBe('1.0.0')

    // Nouvelle demande : dernière version publiée.
    const c = await planViaApi(alice, 'vitrine', 'D5 : demande en 1.1.0')
    expect(c.plan.bundle.template.version).toBe('1.1.0')

    // B approuvé, puis 1.0.0 retirée avant le démarrage : bloqué, plan intact, nouvelle révision exigée.
    compose('stop', 'worker')
    try {
      await logout(page)
      await login(page, 'alice')
      await approveAsBruno(page, b.ref)
      await logout(page)
      await login(page, 'pub')
      await templateAction(page, '1.0.0', 'withdraw', 'D5 : retrait de démonstration')
      expect(await versionStatus(pub, '1.0.0')).toBe('withdrawn')
    } finally {
      compose('start', 'worker')
    }
    await expect.poll(async () => (await alice.call('GET', `/changes/${b.id}`)).body.state,
      { timeout: 300_000, intervals: [2000] }).toBe('blocked')
    const bDone = (await alice.call('GET', `/changes/${b.id}`)).body
    expect([bDone.plan.bundle_digest, bDone.plan.status]).toEqual([b.plan.bundle_digest, 'ready'])
    expect(bDone.current_revision_no).toBe(b.current_revision_no)
    const jobs = (await alice.call('GET', `/changes/${b.id}/jobs`)).body
    const apply = jobs.find((j: any) => j.kind === 'tf_apply')
    expect([apply.state, apply.blocked_reason, apply.dispatched_at]).toEqual(['blocked', 'resolution_no_longer_admissible', null])

    await logout(page)
    await login(page, 'alice')
    await page.goto(`/demandes/${b.id}`)
    await waitState(page, /Bloqu/)
    await expectAccessible(page, 'D5 exécution bloquée')
  } finally {
    await alice.dispose()
    await pub.dispose()
  }
})
