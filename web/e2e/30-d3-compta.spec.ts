import { api, describeByForm, expect, expectAccessible, login, press, test } from './helpers'

// D3 (T33) : compta/dev n'a aucun réseau autorisé. Blocage expliqué avec le référent, sans question
// technique ni bascule vers un autre environnement.
test('D3 — Chloé dans Compta : blocage expliqué, aucune bascule (T33)', async ({ page }) => {
  await login(page, 'chloe')
  const newRequest = page.getByRole('link', { name: 'Nouvelle demande', exact: true })
  await expect(newRequest).toHaveCount(1)
  await press(newRequest)
  await describeByForm(page)

  const block = page.getByTestId('block')
  await expect(block).toHaveCount(1)
  await expect(block).toHaveText("Aucun réseau n'est disponible pour ce profil. Contactez le référent du projet Compta (Chloé).")
  await expect(page.getByTestId('submit')).toHaveCount(0)
  await expect(page.getByTestId('review').locator('input, select, textarea')).toHaveCount(0)
  await expect(page.getByTestId('plan-details')).toHaveCount(0)
  await expectAccessible(page, 'D3 blocage')

  const cid = new URL(page.url()).searchParams.get('change')!
  const view = (await api(page, 'GET', `/changes/${cid}`)).body
  expect(view.revision.outcome).toBe('blocked')
  expect(view.revision.blocks.map((b: any) => b.code)).toEqual(['no_authorized_network'])
  expect(view.revision.environment.slug).toBe('dev')
  expect(view.revision.missing).toEqual([])
  expect(view.allowed_actions.map((a: any) => a.action)).not.toContain('submit')
})
