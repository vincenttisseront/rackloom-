import { expect, expectAccessible, login, logout, session, test } from './helpers'

// F08 : aperçu sans mutation ; conflit de propriété visible et bloque l'import.
test('F08 — adoption : aperçu sans mutation, conflit bloquant', async ({ page }) => {
  test.setTimeout(120_000)
  const alice = await session('alice')
  try {
    const candidates = await alice.call('GET', '/projects/vitrine/adoptions/candidates?environment=dev')
    expect(candidates.status).toBe(200)
    expect(candidates.body.fictional).toBe(true)
    const orphan = candidates.body.candidates.find((c: any) => c.vm_stable_id === 'vm-9001')
    const external = candidates.body.candidates.find((c: any) => c.vm_stable_id === 'vm-9002')
    expect(orphan).toBeTruthy()
    expect(external?.conflict).toBe(true)

    const enrolled = await alice.call('POST', '/projects/vitrine/adoptions', {
      environment: 'dev',
      vm_stable_id: 'vm-9001',
    })
    expect(enrolled.status).toBe(200)
    expect(enrolled.body.observation_fictional).toBe(true)

    const preview = await alice.call('POST', `/adoptions/${enrolled.body.id}/preview`)
    expect(preview.status).toBe(200)
    expect(preview.body.preview_result.mutation).toBe(false)
    expect(preview.body.preview_result.import_executed).toBe(false)

    const conflictEnroll = await alice.call('POST', '/projects/vitrine/adoptions', {
      environment: 'dev',
      vm_stable_id: 'vm-9002',
    })
    expect(conflictEnroll.status).toBe(200)
    expect(conflictEnroll.body.state).toBe('blocked_conflict')
    const conflictPropose = await alice.call('POST', `/adoptions/${conflictEnroll.body.id}/propose-import`)
    expect(conflictPropose.status).toBe(409)
    expect(conflictPropose.body.code).toBe('ownership_conflict')

    await login(page, 'alice')
    await page.goto(`/projets/vitrine/adoption`)
    await expect(page.getByTestId('adoption-candidates')).toBeVisible()
    await expect(page.getByTestId('candidate-vm-9002')).toContainText('conflit')
    await expectAccessible(page, 'F08 adoption')
    await logout(page)
  } finally {
    await alice.dispose()
  }
})
