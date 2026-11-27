import { expect, expectAccessible, login, logout, session, test } from './helpers'

// F20 : inventaire logiciel — avis hors ligne, export périmètre, pas de verdict amont seul.
test('F20 — inventaire logiciel et export filtré', async ({ page }) => {
  test.setTimeout(120_000)
  const admin = await session('admin')
  const alice = await session('alice')
  const chloe = await session('chloe')
  try {
    const imported = await admin.call('POST', '/admin/software-advisories/import', { fixture: true })
    expect(imported.status).toBe(200)
    expect(imported.body.advisory_count).toBeGreaterThan(0)

    const inv = await alice.call('GET', '/projects/vitrine/software-inventory')
    expect(inv.status).toBe(200)
    expect(inv.body.provenance.never_upstream_alone).toBe(true)

    const denied = await chloe.call('GET', '/projects/vitrine/software-inventory')
    expect(denied.status).toBe(404)

    await login(page, 'alice')
    await page.goto('/projets/vitrine')
    const panel = page.getByTestId('software-inventory-panel')
    await expect(panel).toBeVisible()
    await expect(page.getByTestId('software-provenance')).toContainText('jamais sur la seule version amont')
    await expectAccessible(page, 'F20 inventaire logiciel')
    await logout(page)
  } finally {
    await admin.dispose()
    await alice.dispose()
    await chloe.dispose()
  }
})
