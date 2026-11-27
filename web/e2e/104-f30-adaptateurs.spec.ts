import { expect, expectAccessible, login, logout, press, session, test } from './helpers'

// F30 : adaptateurs — soumission, pas d'install sans approbation, suite de contrat, accessibilité.
test('F30 — adaptateurs : soumission, contrat, accessibilité', async ({ page }) => {
  test.setTimeout(120_000)
  const admin = await session('admin')
  try {
    const contract = await admin.call('GET', '/admin/adapters/contract')
    expect(contract.status).toBe(200)
    expect(contract.body.ok).toBe(true)

    const submitted = await admin.call('POST', '/admin/adapters/submit', { demo_reference: true })
    expect(submitted.status).toBe(200)
    expect(submitted.body.status).toBe('submitted')

    const installEarly = await admin.call('POST', `/admin/adapters/${submitted.body.id}/install`)
    expect(installEarly.status).toBe(409)
    expect(installEarly.body.code).toBe('adapter_not_approved')

    await login(page, 'admin')
    await page.goto('/admin/adaptateurs')
    await expect(page.getByRole('heading', { name: /Adaptateurs/ })).toBeVisible()
    await expect(page.getByTestId('contract-verdict')).toContainText('verte')
    await expect(page.getByTestId(`adapter-${submitted.body.slug}-${submitted.body.version}`)).toBeVisible()
    await press(page.getByTestId(`adapter-${submitted.body.slug}-${submitted.body.version}`))
    await expect(page.getByTestId('adapter-detail')).toBeVisible()
    await expect(page.getByTestId('adapter-status')).toContainText('soumis')
    await expect(page.getByTestId('adapter-install')).toHaveCount(0)
    await expectAccessible(page, 'F30 adaptateurs')
    await logout(page)
  } finally {
    await admin.dispose()
  }
})
