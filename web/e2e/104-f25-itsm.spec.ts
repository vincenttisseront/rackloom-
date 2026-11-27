import { expect, expectAccessible, login, logout, session, test } from './helpers'

// F25 : ticket ITSM fictif relié, panneau UI, notification vers page authentifiée.
test('F25 — ITSM et notifications', async ({ page }) => {
  test.setTimeout(180_000)
  const alice = await session('alice')
  try {
    const changes = await alice.call('GET', '/projects/vitrine/changes')
    expect(changes.status).toBe(200)
    const changeId = changes.body?.[0]?.id as string | undefined
    expect(changeId, 'au moins une demande vitrine en démo').toBeTruthy()

    const sync = await alice.call('POST', `/changes/${changeId}/itsm/sync`, {})
    expect(sync.status).toBe(200)
    expect(sync.body.ticket?.external_key).toMatch(/^RLM-/)
    expect(sync.body.ticket?.rackloom_url).toContain(`/demandes/${changeId}`)
    expect(sync.body.simulated).toBe(true)

    // Re-sync idempotent : même ticket.
    const again = await alice.call('POST', `/changes/${changeId}/itsm/sync`, {})
    expect(again.status).toBe(200)
    expect(again.body.ticket?.id).toBe(sync.body.ticket.id)

    const notes = await alice.call('GET', '/notifications?unread=true')
    expect(notes.status).toBe(200)
    expect(notes.body.can_approve).toBe(false)
    expect(notes.body.items.some((n: { href: string }) => n.href === `/demandes/${changeId}`)).toBe(true)

    // Répondre avec approve → refusé (canal entrant).
    const nid = notes.body.items[0].id
    const forbidden = await alice.call('POST', `/notifications/${nid}/respond`, {
      action: 'approve',
      decision: 'approved',
    })
    expect(forbidden.status).toBe(403)
    expect(forbidden.body.code).toBe('approval_forbidden')

    await login(page, 'alice')
    await page.goto(`/demandes/${changeId}`)
    await expect(page.getByTestId('itsm-panel')).toBeVisible()
    await expect(page.getByTestId('itsm-tickets')).toContainText('RLM-')
    await expectAccessible(page, 'F25 ITSM demande')

    await page.goto('/')
    await expect(page.getByTestId('notifications-panel')).toBeVisible()
    await expect(page.getByTestId('notification-item').first()).toBeVisible()
    await logout(page)
  } finally {
    await alice.dispose()
  }
})
