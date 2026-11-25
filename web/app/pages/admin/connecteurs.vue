<script setup lang="ts">
const api = useApi()
const connectors = ref<any[]>([])
const error = ref<string | null>(null)
const CAPS = ['configure', 'test_connection', 'inventory', 'plan', 'execute', 'track', 'cancel']
const CAP_LABELS: Record<string, string> = { configure: 'Configurer', test_connection: 'Tester', inventory: 'Inventaire', plan: 'Plan', execute: 'Exécuter', track: 'Suivre', cancel: 'Annuler' }

onMounted(async () => {
  try {
    connectors.value = await api.get('/connectors')
  } catch (e) {
    error.value = errorText(e)
  }
})
</script>

<template>
  <div>
    <div class="page-head"><h1>Administration › Connecteurs</h1></div>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>
    <section class="card">
      <table>
        <thead>
          <tr><th scope="col">Connecteur</th><th v-for="c in CAPS" :key="c" scope="col">{{ CAP_LABELS[c] }}</th><th scope="col">Erreurs</th></tr>
        </thead>
        <tbody>
          <tr v-for="k in connectors" :key="k.id">
            <td><strong>{{ k.slug }}</strong> <SimBadge v-if="k.simulated" /></td>
            <td v-for="c in CAPS" :key="c">
              <template v-if="k.capabilities[c]?.available">
                <SimBadge v-if="k.capabilities[c].mode === 'simulated'" />
                <span v-else class="badge ok">{{ k.capabilities[c].mode === 'request' ? 'demande' : 'oui' }}</span>
                <div v-if="k.capabilities[c].note" class="small muted">{{ k.capabilities[c].note }}</div>
              </template>
              <span v-else class="badge neutral">non disponible</span>
            </td>
            <td>{{ k.error_count }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
