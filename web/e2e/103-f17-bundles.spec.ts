import { expect, expectAccessible, login, logout, press, session, test } from './helpers'

// F17 : bundles hors ligne — contenu exact affiché ; import admin ; accessibilité.
test('F17 — bundles hors ligne : inspect, contenu exact, accessibilité', async ({ page }) => {
  test.setTimeout(120_000)
  const admin = await session('admin')
  try {
    const built = await admin.call('POST', '/admin/offline-bundles/build-demo', {
      slug: 'e2e-offline',
      version: '1.0.0',
      title: 'Lot e2e F17',
    })
    expect(built.status).toBe(200)
    expect(built.body.package_b64).toBeTruthy()

    const inspected = await admin.call('POST', '/admin/offline-bundles/inspect', {
      package_b64: built.body.package_b64,
    })
    expect(inspected.status).toBe(200)
    expect(inspected.body.items.length).toBe(5)
    expect(inspected.body.status).toBe('inspected')

    // Lot altéré d'un octet → refus.
    const raw = Buffer.from(built.body.package_b64, 'base64')
    raw[raw.length - 1] = raw[raw.length - 1] ^ 0x01
    const tampered = await admin.call('POST', '/admin/offline-bundles/inspect', {
      package_b64: raw.toString('base64'),
    })
    expect(tampered.status).toBe(422)
    expect(tampered.body.code).toBe('bundle_tampered')

    await login(page, 'admin')
    await page.goto('/admin/bundles')
    await expect(page.getByRole('heading', { name: /Bundles hors ligne/ })).toBeVisible()
    await press(page.getByTestId(`open-bundle-${inspected.body.slug}-${inspected.body.version}`))
    await expect(page.getByTestId('bundle-detail')).toBeVisible()
    await expect(page.getByTestId('bundle-items')).toBeVisible()
    await expect(page.getByTestId('bundle-digest')).toContainText(inspected.body.content_digest.slice(0, 12))
    await expectAccessible(page, 'F17 bundles hors ligne')
    await logout(page)
  } finally {
    await admin.dispose()
  }
})
