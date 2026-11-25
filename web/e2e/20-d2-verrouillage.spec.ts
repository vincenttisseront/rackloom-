import {
  D1, LOCKED, api, ciSubmit, describeByForm, expect, expectAccessible, login, need, press, setAssistant, test,
  typeInto,
} from './helpers'

// D2 (T32) : réglages visibles en lecture seule ; les fixer par l'API, le chat ou un client CI est refusé
// (422 locked_field), y compris avec la valeur déjà résolue, et aucune version n'est créée.
test('D2 — réglages en lecture seule, toute tentative de les fixer refusée (T32)', async ({ page }) => {
  test.setTimeout(240_000)
  await login(page, 'alice')
  await page.goto('/projets/vitrine/nouvelle')
  await describeByForm(page)
  const details = page.getByTestId('plan-details')
  await press(details.locator('summary'))
  await expect(details).toHaveAttribute('open', '')
  await expect(details.locator('input, select, textarea, button, [contenteditable="true"]')).toHaveCount(0)
  await expectAccessible(page, 'D2 détails du plan')

  const cid = new URL(page.url()).searchParams.get('change')!
  const view = (await api(page, 'GET', `/changes/${cid}`)).body
  const resolved: Record<string, unknown> = Object.fromEntries(
    view.revision.details.groups.flatMap((g: any) => g.values.map((v: any) => [v.pointer, v.value])))
  const values = { network: resolved['/placement/network'], vcpu: resolved['/instances/0/vcpu'], image: resolved['/placement/image'] }
  for (const [key, value] of Object.entries(values)) {
    expect(value, `valeur résolue ${key}`).toBeTruthy()
    await expect(details).toContainText(String(value))
  }

  // API, avec la valeur résolue, dans le besoin, dans ses entrées et à côté du besoin.
  for (const [key, value] of Object.entries(values)) {
    for (const body of [
      { need: need({ [key]: value }), base_version_no: view.current_version_no },
      { need: need({}, { [key]: value }), base_version_no: view.current_version_no },
      { need: need(), base_version_no: view.current_version_no, [key]: value },
    ]) {
      const r = await api(page, 'POST', `/changes/${cid}/versions`, body)
      expect([r.status, r.body?.code, r.body?.message], `${key} ${JSON.stringify(body).slice(0, 80)}`)
        .toEqual([422, 'locked_field', LOCKED])
    }
  }

  // Client CI réel, depuis l'hôte.
  const ci = ciSubmit('alice', 'vitrine', need({}, { vcpu: values.vcpu }))
  expect(ci.code).toBe(1)
  expect(ci.out).toContain('"locked_field"')

  // Chat : le scénario override_attempt propose une valeur technique, rejetée sans rien modifier.
  await setAssistant('fake', 'override_attempt')
  try {
    await page.goto(`/projets/vitrine/nouvelle?change=${cid}`)
    await typeInto(page.getByLabel("Message pour l'assistant"), D1)
    await page.keyboard.press('Enter')
    const reply = page.locator('.bubble.bot').last()
    await expect(reply).toContainText("Suggestion de l'assistant rejetée")
    await expect(reply).toContainText('Aucune valeur n\'a été modifiée')
    await expect(reply.getByRole('button', { name: 'Reporter dans le formulaire' })).toHaveCount(0)
  } finally {
    await setAssistant('fake', 'nominal')
  }

  const after = (await api(page, 'GET', `/changes/${cid}`)).body
  expect(after.current_version_no).toBe(view.current_version_no)
  expect(after.revision.resolution_digest).toBe(view.revision.resolution_digest)
})
