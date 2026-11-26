/**
 * F31 : le job préflight précède le plan ; l'UI l'affiche.
 * Échecs TLS / state (`tls_untrusted`, `state_unreachable`) : pytest `test_f31_preflight`
 * (serveurs jetables) ; le bandeau `data-testid=change-reason` affiche le motif sans identifiant.
 */
import { expect, login, logout, need, session, test } from './helpers'

test('F31 préflight réussi puis plan prêt', async ({ page }) => {
  const alice = await session('alice')
  const change = (await alice.call('POST', '/projects/vitrine/changes', { title: 'F31 préflight' })).body
  const v = await alice.call('POST', `/changes/${change.id}/versions`, {
    need: need(), base_version_no: 0, source: 'webui',
  })
  expect(v.status).toBe(200)
  expect((await alice.call('POST', `/changes/${change.id}/submit`, { revision_no: v.body.revision_no })).status)
    .toBe(200)

  await expect.poll(async () => (await alice.call('GET', `/changes/${change.id}`)).body.state, {
    timeout: 180_000, intervals: [1000],
  }).toBe('plan_ready')

  const jobs = (await alice.call('GET', `/changes/${change.id}/jobs`)).body
  expect(jobs.map((j: any) => j.kind).slice(0, 2)).toEqual(['preflight', 'tf_plan'])
  expect(jobs[0].class).toBe('read')
  expect(jobs[0].state).toBe('succeeded')

  await login(page, 'alice')
  await page.goto(`/demandes/${change.id}`)
  await expect(page.getByRole('heading', { level: 3, name: 'Préflight' })).toBeVisible()
  await expect(page.getByText('Préflight : accès vCenter')).toBeVisible()
  await logout(page)
  await alice.dispose()
})
