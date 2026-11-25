import {
  expect, expectAccessible, login, logout, planViaApi, press, session, test,
} from './helpers'

test('F55 — boîte À moi d\'agir (demandeur, approbateur, auditeur)', async ({ page }) => {
  test.setTimeout(300_000)
  const alice = await session('alice')
  const planned = await planViaApi(alice, 'vitrine', 'F55 : À moi d\'agir')

  // Demandeur : en attente d'approbation, pas d'action d'approbation.
  await login(page, 'alice')
  await page.goto('/')
  await expect(page.getByTestId('inbox')).toBeVisible()
  const aliceWaiting = page.getByTestId('inbox-waiting-item').filter({ hasText: planned.ref })
  await expect(aliceWaiting).toBeVisible()
  await expect(aliceWaiting.getByTestId('inbox-waiting-blocker')).toContainText(/approbation/i)
  await expect(page.getByTestId('inbox-mine-item').filter({ hasText: planned.ref })).toHaveCount(0)
  await expectAccessible(page, 'F55 Alice')

  // Approbateur : examiner le plan.
  await logout(page)
  await login(page, 'bruno')
  await page.goto('/')
  const brunoMine = page.getByTestId('inbox-mine-item').filter({ hasText: planned.ref })
  await expect(brunoMine).toBeVisible()
  await expect(brunoMine.getByTestId('inbox-next-action')).toContainText('Examiner le plan')
  await press(brunoMine.getByTestId('inbox-next-action'))
  await expect(page.getByTestId('plan-panel')).toBeVisible()
  await press(page.getByTestId('approve'))
  await expect(page.getByText('Plan approuvé : exécution du plan approuvé.')).toBeVisible()

  // Après décision : plus dans « À moi d'agir » ; second clic → 409 via API.
  await page.goto('/')
  await expect(page.getByTestId('inbox-mine-item').filter({ hasText: planned.ref })).toHaveCount(0)
  const again = await session('bruno')
  const plan = (await again.call('GET', `/changes/${planned.id}`)).body.plan
  const dup = await again.call('POST', `/changes/${planned.id}/approvals`, {
    plan_id: plan.id, bundle_digest: plan.bundle_digest, decision: 'approved',
  })
  expect(dup.status).toBe(409)
  await again.dispose()

  // Auditeur : voit l'attente (ou l'état), ne peut pas approuver.
  await logout(page)
  await login(page, 'david')
  await page.goto('/')
  await expect(page.getByTestId('inbox')).toBeVisible()
  // Demande déjà approuvée : plus « plan prêt » ; l'auditeur ne doit pas avoir d'action mine sur approve.
  await expect(page.getByTestId('inbox-mine-item').filter({ hasText: 'Examiner le plan' })).toHaveCount(0)
  await expectAccessible(page, 'F55 David')

  // Révocation : nouveau plan, puis retrait du rôle approbateur → entrée absente + 403.
  const planned2 = await planViaApi(alice, 'vitrine', 'F55 révocation')
  const admin = await session('admin')
  const users = (await admin.call('GET', '/users')).body as {
    email: string; roles: { id: string; role: string }[]
  }[]
  const brunoUser = users.find(u => u.email === 'bruno@demo.invalid')
  const brunoBinding = brunoUser?.roles.find(r => r.role === 'approver')
  expect(brunoBinding, JSON.stringify(brunoUser)).toBeTruthy()
  expect((await admin.call('POST', `/role-bindings/${brunoBinding!.id}/revoke`)).status).toBe(200)
  const brunoId = (await admin.call('GET', '/users')).body.find((u: any) => u.email === 'bruno@demo.invalid').id
  const vitrineId = (await alice.call('GET', '/projects')).body.find((p: any) => p.slug === 'vitrine').id
  const grant = await admin.call('POST', '/role-bindings', {
    user_id: brunoId,
    role: 'auditor',
    project_id: vitrineId,
  })
  expect(grant.status, JSON.stringify(grant.body)).toBe(200)
  await admin.dispose()

  const brunoAfter = await session('bruno')
  const inbox = (await brunoAfter.call('GET', '/inbox')).body as {
    mine: { change_id: string }[]; waiting: { change_id: string }[]
  }
  expect(inbox.mine.some(i => i.change_id === planned2.id)).toBe(false)
  expect(inbox.waiting.some(i => i.change_id === planned2.id)).toBe(true)
  const plan2 = (await alice.call('GET', `/changes/${planned2.id}`)).body.plan
  const denied = await brunoAfter.call('POST', `/changes/${planned2.id}/approvals`, {
    plan_id: plan2.id, bundle_digest: plan2.bundle_digest, decision: 'approved',
  })
  expect(denied.status).toBe(403)
  await brunoAfter.dispose()
  await alice.dispose()
})
