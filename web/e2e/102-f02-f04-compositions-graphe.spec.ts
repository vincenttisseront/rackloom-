import { expect, expectAccessible, login, session, test } from './helpers'

test('F04 — graphe de dépendances et impact filtré', async ({ page }) => {
  test.setTimeout(120_000)
  const alice = await session('alice')
  const chloe = await session('chloe')

  const rebuilt = await alice.call('POST', '/dependency-graph/rebuild', {})
  expect(rebuilt.status).toBe(200)
  expect(rebuilt.body.node_count).toBeGreaterThan(0)

  const graph = await alice.call('GET', '/dependency-graph')
  expect(graph.status).toBe(200)
  const network = (graph.body.nodes || []).find((n: any) => n.kind === 'network')
  expect(network, 'au moins un réseau dans le graphe').toBeTruthy()

  const impact = await alice.call('GET', `/dependency-graph/impact/network/${network.stable_id}`)
  expect(impact.status).toBe(200)
  expect(impact.body.resource.stable_id).toBe(network.stable_id)

  // Chloé (compta) : pas de fuite des VM vitrine.
  const other = await chloe.call('GET', `/dependency-graph/impact/network/${network.stable_id}`)
  expect(other.status).toBe(200)
  const aliceVmNames = new Set(
    (impact.body.dependents || []).filter((d: any) => d.kind === 'vm').map((d: any) => d.name),
  )
  for (const d of other.body.dependents || []) {
    expect(aliceVmNames.has(d.name)).toBe(false)
  }

  await login(page, 'alice')
  await page.goto('/dependances')
  await expect(page.getByTestId('impact-panel')).toBeVisible({ timeout: 30_000 })
  await page.getByTestId('impact-stable-id').fill(network.stable_id)
  await page.getByTestId('impact-submit').click()
  await expect(page.getByTestId('impact-result')).toBeVisible({ timeout: 20_000 })
  await expectAccessible(page, 'F04 graphe')
})
