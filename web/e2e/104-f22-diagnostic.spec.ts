import { expect, expectAccessible, login, logout, session, test } from './helpers'

// F22 : dossier de diagnostic — génération filtrée, téléchargement, hors périmètre 404.
test('F22 — dossier de diagnostic partageable', async ({ page }) => {
  test.setTimeout(180_000)
  const alice = await session('alice')
  const chloe = await session('chloe')
  try {
    // Créer une demande minimale via l'API Alice pour avoir une page demande.
    const created = await alice.call('POST', '/projects/vitrine/changes', {
      title: 'F22 diagnostic e2e',
    })
    expect(created.status).toBe(200)
    const changeId = created.body.id as string

    const packRes = await alice.call('POST', `/changes/${changeId}/diagnostic-packs`, {
      include_summary: true,
      symptoms: ['sonde HTTP en échec'],
    })
    expect(packRes.status).toBe(200)
    expect(packRes.body.summary).toBeTruthy()
    expect(JSON.stringify(packRes.body)).not.toContain('RLCANARY-')

    const dl = await alice.call('GET', `/diagnostic-packs/${packRes.body.id}/download`)
    expect(dl.status).toBe(200)
    expect(JSON.stringify(dl.body)).not.toContain('RLCANARY-')
    expect(dl.body.schema).toBe('rackloom.diagnostic_pack/v1')

    const denied = await chloe.call('GET', `/diagnostic-packs/${packRes.body.id}/download`)
    expect(denied.status).toBe(404)

    await login(page, 'alice')
    await page.goto(`/demandes/${changeId}`)
    await expect(page.getByTestId('diagnostic-pack')).toBeVisible()
    await page.getByTestId('create-diagnostic').click()
    await expect(page.getByTestId('diagnostic-notice')).toBeVisible({ timeout: 15_000 })
    await expect(page.getByTestId('diagnostic-list')).toBeVisible()
    await expectAccessible(page, 'F22 diagnostic')
    await logout(page)
  } finally {
    await alice.dispose()
    await chloe.dispose()
  }
})
