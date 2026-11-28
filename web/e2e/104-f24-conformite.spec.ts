import { expect, expectAccessible, login, logout, session, test } from './helpers'

// F24 : profils publiés ; scan lecture seule ; remédiation = brouillon, pas d'écriture.
test('F24 — conformité : profils et panneau machine', async ({ page }) => {
  test.setTimeout(120_000)
  const alice = await session('alice')
  try {
    const profiles = await alice.call('GET', '/compliance/profiles')
    expect(profiles.status).toBe(200)
    expect(profiles.body.some((p: { slug: string }) => p.slug === 'ubuntu-web-baseline')).toBe(true)

    await login(page, 'alice')
    // La page machines/[id] exige un hôte existant ; on vérifie l'API connecteur Rudder.
    const connectors = await alice.call('GET', '/connectors')
    expect(connectors.status).toBe(200)
    const rudder = connectors.body.find((c: { slug: string }) => c.slug === 'rudder-fake')
    expect(rudder).toBeTruthy()
    expect(rudder.mode === 'simulated' || rudder.simulated === true).toBe(true)
    expect(rudder.capabilities?.execute?.available).toBe(false)

    await logout(page)
    await login(page, 'admin')
    await page.goto('/admin/connecteurs')
    await expect(page.getByTestId('rudder-admin')).toBeVisible()
    await expect(page.getByTestId('rudder-config-form')).toBeVisible()
    await expectAccessible(page, 'RAC-36 Rudder admin')
    await logout(page)
  } finally {
    await alice.dispose()
  }
})
