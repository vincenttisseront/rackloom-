import { expect, expectAccessible, login, logout, press, session, test } from './helpers'

// F01 : promotion UI distincte de la publication ; aucun job créé.
// Après D5 (90) : 1.1.0 est déjà publiée — on vérifie l'UI de promotion sans republier.
// Avant D5, publier 1.1.0 polluait le seed partagé (latest_promoted → 1.1.0).
test('F01 — publier puis promouvoir sans créer de job', async ({ page }) => {
  test.setTimeout(180_000)
  const pub = await session('pub')
  try {
    const before = (await pub.call('GET', '/admin/templates')).body
      .find((t: any) => t.slug === 'ubuntu-web')
    expect(before.versions.some((v: any) =>
      v.version === '1.1.0' && (v.status === 'validated' || v.status === 'published'))).toBeTruthy()

    const status = before.versions.find((v: any) => v.version === '1.1.0')?.status
    await login(page, 'pub')
    await page.goto('/admin/templates')
    await press(page.getByTestId('open-ubuntu-web-1.1.0'))
    await expect(page.getByTestId('template-version')).toBeVisible()

    if (status === 'validated') {
      await page.getByLabel('Motif (obligatoire)').fill('F01 : publication catalogue')
      await press(page.getByTestId('action-publish'))
      await expect(page.getByTestId('error')).toHaveCount(0)
    }

    // Après publication filesystem : promotions auto ; si un kind manque, promouvoir.
    const detail = (await pub.call('GET', '/admin/templates/ubuntu-web/versions/1.1.0')).body
    const missing = (detail.contract.deployment_policy.admissible_environment_kinds as string[])
      .filter((k: string) => !(detail.promotions || []).some((p: any) => p.environment_kind === k))
    if (missing.length) {
      await page.getByLabel('Motif (obligatoire)').fill(`F01 : promotion ${missing[0]}`)
      await press(page.getByTestId(`action-promote:${missing[0]}`))
      await expect(page.getByTestId('error')).toHaveCount(0)
      await expect(page.getByTestId('promotions')).toContainText(missing[0] === 'preprod' ? 'préprod' : missing[0])
    } else {
      await expect(page.getByTestId('promotions')).toBeVisible()
    }

    await expectAccessible(page, 'F01 administration templates')
    await logout(page)
  } finally {
    await pub.dispose()
  }
})
