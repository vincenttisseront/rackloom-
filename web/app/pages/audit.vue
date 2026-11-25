<script setup lang="ts">
const api = useApi()
const data = ref<any>(null)
const error = ref<string | null>(null)
onMounted(async () => {
  try {
    data.value = await api.get('/audit?limit=300')
  } catch (e) {
    error.value = errorText(e)
  }
})
const show = (v: any) => (v && Object.keys(v).length ? JSON.stringify(v) : '—')
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>Audit</h1>
        <p class="small muted">Journal en ajout seul, chaîné par hachage. Aucun secret n'y figure.</p>
      </div>
      <span v-if="data" class="badge" :class="data.chain.ok ? 'ok' : 'bad'" data-testid="audit-chain">
        {{ data.chain.ok ? `Chaîne intègre (${data.chain.checked} entrées)` : `Chaîne rompue à l'entrée ${data.chain.first_bad_id}` }}
      </span>
    </div>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>
    <section v-if="data" class="card">
      <table>
        <thead><tr><th scope="col">Date</th><th scope="col">Acteur</th><th scope="col">Action</th><th scope="col">Cibles</th><th scope="col">Versions</th><th scope="col">Décision</th><th scope="col">Résultat</th></tr></thead>
        <tbody>
          <tr v-for="e in data.entries" :key="e.id">
            <td class="small">{{ dateTime(e.at) }}</td>
            <td class="small">{{ e.actor_type }}<div class="muted mono">{{ e.actor_id }}</div></td>
            <td><code>{{ e.action }}</code></td>
            <td class="small mono">{{ show(e.targets) }}</td>
            <td class="small mono">{{ show(e.versions) }}</td>
            <td class="small mono">{{ show(e.policy_decision) }}</td>
            <td class="small">{{ e.outcome }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
