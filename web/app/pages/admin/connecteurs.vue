<script setup lang="ts">
const api = useApi()
const connectors = ref<any[]>([])
const assistant = ref<{
  adapter: string
  scenario: string
  simulated: boolean
  adapters: string[]
  scenarios: string[]
  prompt_id?: string
  routing?: any
} | null>(null)
const adapter = ref('fake')
const scenario = ref('nominal')
const analysisDisabled = ref(false)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const evalBusy = ref(false)
const lastEval = ref<any | null>(null)
const rudderBusy = ref(false)
const rudderForm = reactive({
  url: '',
  caPem: '',
  defaultNodeId: '',
  token: '',
})
const rudderTest = ref<any | null>(null)
const CAPS = ['configure', 'test_connection', 'inventory', 'plan', 'execute', 'track', 'cancel']
const CAP_LABELS: Record<string, string> = { configure: 'Configurer', test_connection: 'Tester', inventory: 'Inventaire', plan: 'Plan', execute: 'Exécuter', track: 'Suivre', cancel: 'Annuler' }
const ADAPTERS: Record<string, string> = { fake: 'Adaptateur fictif (déterministe)', ollama: 'Ollama (modèle local)', disabled: 'Désactivé' }

const rudderConnectors = computed(() => connectors.value.filter((c) => c.kind === 'rudder'))
const selectedRudderId = ref<string | null>(null)
const selectedRudder = computed(() => rudderConnectors.value.find((c) => c.id === selectedRudderId.value) || rudderConnectors.value[0] || null)

function syncRudderForm() {
  const c = selectedRudder.value
  if (!c) return
  selectedRudderId.value = c.id
  rudderForm.url = c.config?.url || ''
  rudderForm.defaultNodeId = c.config?.default_node_id || ''
  rudderForm.caPem = ''
  rudderForm.token = ''
  rudderTest.value = c.last_test_result || null
}

async function loadConnectors() {
  connectors.value = await api.get('/connectors')
  if (!selectedRudderId.value && rudderConnectors.value.length) {
    selectedRudderId.value = rudderConnectors.value[0].id
  }
  syncRudderForm()
}

async function loadAssistant() {
  assistant.value = await api.get('/admin/assistant')
  adapter.value = assistant.value!.adapter
  scenario.value = assistant.value!.scenario
  const disabled = assistant.value?.routing?.disabled_roles || []
  analysisDisabled.value = disabled.includes('analysis')
}

async function loadEval() {
  const listed = await api.get('/admin/assistant/eval')
  lastEval.value = listed.items?.[0] || null
}

onMounted(async () => {
  try {
    await loadConnectors()
    await loadAssistant()
    await loadEval()
  } catch (e) {
    error.value = errorText(e)
  }
})

watch(selectedRudderId, () => syncRudderForm())

async function saveRudderConfig() {
  if (!selectedRudder.value) return
  rudderBusy.value = true
  error.value = null
  notice.value = null
  try {
    const config: Record<string, string> = {}
    if (rudderForm.url) config.url = rudderForm.url
    if (rudderForm.caPem.trim()) config.ca_pem = rudderForm.caPem.trim()
    if (rudderForm.defaultNodeId.trim()) config.default_node_id = rudderForm.defaultNodeId.trim()
    await api.put(`/connectors/${selectedRudder.value.id}`, { config })
    if (rudderForm.token) {
      await api.put(`/connectors/${selectedRudder.value.id}/secrets/token`, { value: rudderForm.token })
      rudderForm.token = ''
    }
    await loadConnectors()
    notice.value = selectedRudder.value.simulated
      ? 'Configuration Rudder fictive enregistrée (aucune valeur secrète affichée).'
      : 'Configuration Rudder enregistrée. Le jeton n’est jamais renvoyé par l’API.'
  } catch (e) {
    error.value = errorText(e)
  } finally {
    rudderBusy.value = false
  }
}

async function testRudder() {
  if (!selectedRudder.value) return
  rudderBusy.value = true
  error.value = null
  notice.value = null
  try {
    rudderTest.value = await api.post(`/connectors/${selectedRudder.value.id}/test`)
    await loadConnectors()
    notice.value = rudderTest.value.status === 'ok'
      ? (rudderTest.value.simulated ? 'Test simulé OK.' : 'TLS Rudder OK.')
      : `Test Rudder : ${rudderTest.value.message || rudderTest.value.status}`
  } catch (e) {
    error.value = errorText(e)
  } finally {
    rudderBusy.value = false
  }
}

async function saveAssistant() {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    await api.put('/admin/assistant', {
      adapter: adapter.value,
      scenario: scenario.value,
      routing: { disabled_roles: analysisDisabled.value ? ['analysis'] : [] },
    })
    await loadAssistant()
    notice.value = `Assistant : ${ADAPTERS[adapter.value]}${adapter.value === 'fake' ? `, scénario « ${scenario.value} »` : ''}${analysisDisabled.value ? ' ; analyse indisponible' : ''}.`
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function runEval() {
  evalBusy.value = true
  error.value = null
  notice.value = null
  try {
    lastEval.value = await api.post('/admin/assistant/eval', {
      adapter: adapter.value === 'disabled' ? undefined : adapter.value,
      scenario: adapter.value === 'fake' ? scenario.value : undefined,
    })
    notice.value = lastEval.value.verdict === 'passed'
      ? `Qualification réussie pour ${lastEval.value.model} (${lastEval.value.prompt_id}).`
      : `Qualification échouée : ${(lastEval.value.failure_codes || []).join(', ') || 'voir le rapport'}.`
  } catch (e) {
    error.value = errorText(e)
  } finally {
    evalBusy.value = false
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
          <tr>
            <th scope="col">Connecteur</th>
            <th v-for="c in CAPS" :key="c" scope="col">{{ CAP_LABELS[c] }}</th>
            <th scope="col">Disjoncteur</th>
            <th scope="col">Erreurs</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="k in connectors" :key="k.id" :data-testid="`connector-${k.slug}`">
            <td><strong>{{ k.slug }}</strong> <SimBadge v-if="k.simulated" /></td>
            <td v-for="c in CAPS" :key="c">
              <template v-if="k.capabilities[c]?.available">
                <SimBadge v-if="k.capabilities[c].mode === 'simulated'" />
                <span v-else class="badge ok">{{ k.capabilities[c].mode === 'request' ? 'demande' : 'oui' }}</span>
                <div v-if="k.capabilities[c].note" class="small muted">{{ k.capabilities[c].note }}</div>
              </template>
              <span v-else class="badge neutral">non disponible</span>
            </td>
            <td>
              <span
                v-if="k.circuit?.state === 'open'"
                class="badge bad"
                data-testid="circuit-open"
                :title="k.circuit.opened_reason || ''"
              >ouvert</span>
              <span v-else class="badge ok" data-testid="circuit-closed">fermé</span>
            </td>
            <td>{{ k.error_count }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section
      v-if="rudderConnectors.length"
      class="card"
      aria-labelledby="rudder-admin-title"
      data-testid="rudder-admin"
    >
      <div class="field-head">
        <h2 id="rudder-admin-title">Rudder (conformité, lecture seule)</h2>
        <SimBadge v-if="selectedRudder?.simulated" />
      </div>
      <p class="small muted">
        Configurez l’URL HTTPS et le jeton API via une référence de secret. Aucune valeur secrète n’est
        jamais renvoyée ni affichée. L’import d’observations alimente F24 ; l’écriture / remédiation Rudder
        reste indisponible. Labo Rudder réel : non vérifié.
      </p>
      <div v-if="rudderConnectors.length > 1" class="field">
        <label for="rudder-connector">Connecteur</label>
        <select id="rudder-connector" v-model="selectedRudderId" data-testid="rudder-connector-select">
          <option v-for="c in rudderConnectors" :key="c.id" :value="c.id">
            {{ c.slug }} ({{ c.mode }})
          </option>
        </select>
      </div>
      <form class="actions" data-testid="rudder-config-form" @submit.prevent="saveRudderConfig">
        <div class="field">
          <label for="rudder-url">URL (https://hôte[:port])</label>
          <input id="rudder-url" v-model="rudderForm.url" type="url" autocomplete="off" data-testid="rudder-url" />
        </div>
        <div class="field">
          <label for="rudder-node">Nœud par défaut (optionnel)</label>
          <input id="rudder-node" v-model="rudderForm.defaultNodeId" type="text" autocomplete="off" data-testid="rudder-node" />
        </div>
        <div class="field">
          <label for="rudder-ca">CA PEM (import)</label>
          <textarea id="rudder-ca" v-model="rudderForm.caPem" rows="4" autocomplete="off" data-testid="rudder-ca" placeholder="-----BEGIN CERTIFICATE-----" />
          <p v-if="selectedRudder?.config?.ca_imported" class="small muted">CA déjà importée (remplacer pour mettre à jour).</p>
        </div>
        <div class="field">
          <label for="rudder-token">
            Jeton API
            <span v-if="selectedRudder?.secrets?.token" class="badge ok">défini</span>
          </label>
          <input
            id="rudder-token"
            v-model="rudderForm.token"
            type="password"
            autocomplete="new-password"
            data-testid="rudder-token"
            placeholder="saisir pour remplacer — jamais affiché ensuite"
          />
        </div>
        <button type="submit" class="primary" :disabled="rudderBusy" data-testid="rudder-save">Enregistrer</button>
        <button type="button" class="secondary" :disabled="rudderBusy" data-testid="rudder-test" @click="testRudder">
          Tester la connexion
        </button>
      </form>
      <p v-if="rudderTest" class="small" data-testid="rudder-test-result">
        Dernier test :
        <span class="badge" :class="rudderTest.status === 'ok' ? 'ok' : 'bad'">{{ rudderTest.status }}</span>
        <span class="muted"> — {{ rudderTest.message }}</span>
        <SimBadge v-if="rudderTest.simulated" />
      </p>
      <p class="small muted">
        Capacités : inventaire lecture seule
        <span v-if="!selectedRudder?.capabilities?.execute?.available">; exécution / écriture non disponible</span>.
        Synchroniser les observations depuis la fiche machine (import Rudder).
      </p>
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
        <div class="field">
          <label>
            <input v-model="analysisDisabled" type="checkbox" data-testid="analysis-disabled" />
            Modèle d'analyse indisponible (F48 : formulaire inchangé, pas de repli cloud)
          </label>
        </div>
        <button type="submit" class="primary" :disabled="busy" data-testid="assistant-save">Enregistrer</button>
      </form>
      <p v-if="assistant.routing" class="small muted" data-testid="assistant-routing">
        Routage serveur : rédaction {{ assistant.routing.routes?.draft?.available === false ? 'indisponible' : 'ok' }},
        analyse {{ assistant.routing.routes?.explain?.available === false ? 'indisponible' : 'ok' }}.
        Aucun sélecteur de modèle pour l'utilisateur standard.
      </p>
      <div class="field-head" style="margin-top: 1.25rem">
        <h3 id="assistant-eval-title">Banc d'évaluation</h3>
      </div>
      <p class="small muted">
        Rejoue des conversations fictives avant de changer de modèle ou de prompt
        (<code>{{ assistant.prompt_id || '—' }}</code>). N'écrit ni demande, ni approbation, ni job.
        Les scénarios <code>defaults_prod</code> et <code>asks_vlan</code> font échouer la qualification.
      </p>
      <div class="actions">
        <button type="button" class="primary" :disabled="evalBusy || adapter === 'disabled'" data-testid="assistant-eval-run" @click="runEval">
          Lancer l'évaluation
        </button>
      </div>
      <div v-if="lastEval" class="small" style="margin-top: 0.75rem" data-testid="assistant-eval-report">
        <span
          class="badge"
          :class="lastEval.verdict === 'passed' ? 'ok' : 'bad'"
          data-testid="assistant-eval-verdict"
        >{{ lastEval.verdict === 'passed' ? 'qualifié' : 'échoué' }}</span>
        <span class="muted"> · {{ lastEval.model }} · {{ lastEval.prompt_id }}</span>
        <span v-if="lastEval.failure_codes?.length" class="muted"> · {{ lastEval.failure_codes.join(', ') }}</span>
        <div class="muted" style="margin-top: 0.25rem">
          questions inutiles {{ lastEval.metrics?.unnecessary_questions ?? 0 }},
          besoin résolu {{ lastEval.metrics?.need_resolved ?? 0 }}/{{ lastEval.metrics?.fixtures ?? 0 }},
          prod par défaut {{ lastEval.metrics?.production_defaults ?? 0 }},
          VLAN {{ lastEval.metrics?.vlan_questions ?? 0 }}
        </div>
      </div>
    </section>
  </div>
</template>
