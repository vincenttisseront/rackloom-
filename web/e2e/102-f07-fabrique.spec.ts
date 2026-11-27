import { expect, expectAccessible, login, logout, press, session, test } from './helpers'

// F07 : fabrique d'images — UI publisher ; promotion refusée si tests en échec (via API).
test('F07 — fabrique : liste, détail seed, accessibilité', async ({ page }) => {
  test.setTimeout(120_000)
  const pub = await session('pub')
  try {
    const listing = await pub.call('GET', '/admin/images')
    expect(listing.status).toBe(200)
    expect(listing.body.some((b: any) => b.slug === 'ubuntu-2404-std')).toBeTruthy()

    // Recette API : échec → pas de promotion prod.
    const source = 'b'.repeat(64)
    const created = await pub.call('POST', '/admin/images', {
      slug: 'e2e-f07-img',
      version: '1.0.0',
      title: 'Image e2e F07',
      source_kind: 'iso_digest',
      source_digest: source,
      os: 'ubuntu-24.04',
      layout_id: 'std-v1',
    })
    expect(created.status).toBe(200)
    expect((await pub.call('POST', '/admin/images/e2e-f07-img/versions/1.0.0/build')).status).toBe(200)
    const failed = await pub.call('POST', '/admin/images/e2e-f07-img/versions/1.0.0/test', { fail: true })
    expect(failed.status).toBe(200)
    expect(failed.body.status).toBe('failed')
    const refuse = await pub.call('POST', '/admin/images/e2e-f07-img/versions/1.0.0/promote', {
      environment_kind: 'prod',
      reason: 'e2e ne doit pas passer',
    })
    expect(refuse.status).toBe(409)
    expect(refuse.body.code).toBe('image_tests_failed')

    await login(page, 'pub')
    await page.goto('/admin/images')
    await expect(page.getByRole('heading', { name: /Fabrique/ })).toBeVisible()
    await press(page.getByTestId('open-ubuntu-2404-std-1.0.0'))
    await expect(page.getByTestId('image-version')).toBeVisible()
    await expect(page.getByTestId('image-tests')).toBeVisible()
    await expect(page.getByTestId('image-promotions')).toContainText('prod')
    await expectAccessible(page, 'F07 fabrique images')
    await logout(page)
  } finally {
    await pub.dispose()
  }
})
