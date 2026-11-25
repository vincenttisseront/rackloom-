<script setup lang="ts">
const api = useApi()
const connectors = ref<any[]>([])
const assistant = ref<{ adapter: string; scenario: string; simulated: boolean; adapters: string[]; scenarios: string[] } | null>(null)
const adapter = ref('fake')
const scenario = ref('nominal')
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const CAPS = ['configure', 'test_connection', 'inventory', 'plan', 'execute', 'track', 'cancel']
const CAP_LABELS: Record<string, string> = { configure: 'Configurer', test_connection: 'Tester', inventory: 'Inventaire', plan: 'Plan', execute: 'Exécuter', track: 'Suivre', cancel: 'Annuler' }
const ADAPTERS: Record<string, string> = { fake: 'Adaptateur fictif (déterministe)', ollama: 'Ollama (modèle local)', disabled: 'Désactivé' }

async function loadAssistant() {
  assistant.value = await api.get('/admin/assistant')
  adapter.value = assistant.value!.adapter
  scenario.value = assistant.value!.scenario
}

onMounted(async () => {
  try {
    connectors.value = await api.get('/connectors')
    await loadAssistant()
  } catch (e) {
    error.value = errorText(e)
  }
})

async function saveAssistant() {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    await api.put('/admin/assistant', { adapter: adapter.value, scenario: scenario.value })
    await loadAssistant()
    notice.value = `Assistant : ${ADAPTERS[adapter.value]}${adapter.value === 'fake' ? `, scénario « ${scenario.value} »` : ''}.`
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div>
    <div class="page-head"><h1>Administration › Connecteurs</h1></div>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>
    <div v-if="notice" class="banner ok" role="status" data-testid="assistant-saved">{{ notice }}</div>
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

    <section v-if="assistant" class="card" aria-labelledby="assistant-admin-title" data-testid="assistant-admin">
      <div class="field-head">
        <h2 id="assistant-admin-title">Assistant</h2>
        <SimBadge v-if="assistant.simulated" label="Assistant fictif" />
      </div>
      <p class="small muted">L'assistant propose un brouillon de besoin ; il ne soumet, n'approuve ni ne déploie rien. Désactivé ou indisponible, le formulaire reste complet.</p>
      <form class="actions" @submit.prevent="saveAssistant">
        <div class="field">
          <label for="assistant-adapter">Adaptateur</label>
          <select id="assistant-adapter" v-model="adapter">
            <option v-for="a in assistant.adapters" :key="a" :value="a">{{ ADAPTERS[a] || a }}</option>
          </select>
        </div>
        <div v-if="adapter === 'fake'" class="field">
          <label for="assistant-scenario">Scénario de démonstration</label>
          <select id="assistant-scenario" v-model="scenario">
            <option v-for="s in assistant.scenarios" :key="s" :value="s">{{ s }}</option>
          </select>
        </div>
        <button type="submit" class="primary" :disabled="busy" data-testid="assistant-save">Enregistrer</button>
      </form>
    </section>
  </div>
</template>
