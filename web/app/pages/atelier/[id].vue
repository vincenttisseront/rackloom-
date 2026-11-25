<script setup lang="ts">
const route = useRoute()
const api = useApi()
const draft = ref<any>(null)
const catalog = ref<any[]>([])
const message = ref('')
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const addSlug = ref('')
const VERIF: Record<string, string> = { not_tested: 'non testé', tested_locally: 'testé localement', lab_validated: 'validé en laboratoire' }
const DECISION: Record<string, string> = { pending: 'en attente', accepted: 'acceptée', rejected: 'refusée', superseded: 'remplacée' }

const pending = computed(() => (draft.value?.suggestions || []).filter((s: any) => s.status === 'pending'))
const available = computed(() => catalog.value.filter(c => !(draft.value?.design.components || []).some((x: any) => x.component === c.slug)))

async function load() {
  draft.value = await api.get(`/studio/drafts/${route.params.id}`)
  catalog.value = await api.get('/studio/catalog')
}
onMounted(async () => {
  try {
    await load()
  } catch (e) {
    error.value = errorText(e)
  }
})

async function run(path: string, body: Record<string, unknown>) {
  busy.value = true
  error.value = null
  try {
    const result = await api.post(path, { row_version: draft.value.row_version, ...body })
    draft.value = result.draft || result
    if (result.status === 'rejected' || result.status === 'unavailable') notice.value = result.notice
    else if (result.reply) notice.value = result.reply
    return result
  } catch (e) {
    error.value = errorText(e)
    await load().catch(() => undefined)
    return null
  } finally {
    busy.value = false
  }
}

function edit(ops: { path: string; value: unknown }[]) {
  return run(`/studio/drafts/${draft.value.id}/edit`, { ops })
}
function decide(id: string, decision: 'accepted' | 'rejected') {
  return run(`/studio/drafts/${draft.value.id}/suggestions/${id}`, { decision })
}
async function assist() {
  const text = message.value.trim()
  if (!text) return
  const result = await run(`/studio/drafts/${draft.value.id}/assist`, { message: text })
  if (result) message.value = ''
}
async function submit() {
  const result = await run(`/studio/drafts/${draft.value.id}/submit`, { digest: draft.value.digest })
  if (result) notice.value = `Version ${result.version} soumise. Elle n'est pas publiée : une autre personne la revoit.`
}
function addComponent() {
  const comp = catalog.value.find(c => c.slug === addSlug.value)
  if (!comp) return
  return edit([{ path: `/components/${comp.slug}`, value: { version: comp.version } }])
}
</script>

<template>
  <div v-if="draft">
    <div class="page-head">
      <div>
        <div class="crumbs"><NuxtLink to="/atelier">Atelier</NuxtLink> › {{ draft.slug }}</div>
        <h1>{{ draft.design.title }} <span class="badge info">{{ draft.status_label }}</span>
          <SimBadge v-if="draft.assistant.simulated" label="Assistant fictif" title="Les suggestions viennent de l'adaptateur fictif, pas d'un modèle." />
        </h1>
        <p class="small muted">{{ draft.slug }} {{ draft.version }} · base {{ draft.base.title }} {{ draft.base.version }} · {{ draft.author }}</p>
      </div>
    </div>
    <div v-if="error" class="banner bad" role="alert" data-testid="error">{{ error }}</div>
    <div v-if="notice" class="banner info" role="status" data-testid="assistant-reply">{{ notice }}</div>
    <div v-if="draft.returned" class="banner warn">Version {{ draft.returned.version }} renvoyée : {{ draft.returned.reason }}</div>

    <section class="card" aria-labelledby="inherit-title">
      <h2 id="inherit-title">Hérité de la base</h2>
      <p class="small">{{ draft.inherited.note }}</p>
      <dl class="kv small">
        <dt>Module</dt><dd class="mono">{{ draft.inherited.terraform }}</dd>
        <dt>Système</dt><dd>{{ draft.inherited.os }}</dd>
        <dt>Environnements</dt><dd>{{ draft.inherited.environments.join(', ') }}</dd>
      </dl>
    </section>

    <section class="card" aria-labelledby="design-title" data-testid="design">
      <h2 id="design-title">Conception</h2>
      <p data-testid="waves-label"><strong>Vagues :</strong> {{ draft.design.waves_label }}</p>
      <button v-if="draft.allowed_actions.edit == null && draft.design.waves.mode !== 'pilot'" type="button" :disabled="busy" @click="edit([{ path: '/waves', value: { mode: 'pilot', pilot_size: 1, wave_size: 1, stop_on_failure: true } }])">
        Pilote, puis vagues
      </button>
      <div v-for="c in draft.design.components" :key="c.component" class="card" style="margin-top: 0.75rem">
        <h3>{{ c.title }} {{ c.version }}
          <span class="badge neutral">{{ VERIF[c.verification_level] || c.verification_level || 'non testé' }}</span>
        </h3>
        <p v-if="c.not_covered?.length" class="small">Non couvert : {{ c.not_covered.join(' ; ') }}</p>
        <dl class="kv small">
          <template v-for="p in c.parameters" :key="p.name">
            <dt><label :for="`p-${c.component}-${p.name}`">{{ p.label }}</label></dt>
            <dd>
              <input v-if="p.type === 'integer'" :id="`p-${c.component}-${p.name}`" type="number" :min="p.min" :max="p.max" :value="p.value" :disabled="draft.allowed_actions.edit != null || busy" @change="edit([{ path: `/components/${c.component}/config/${p.name}`, value: Number(($event.target as HTMLInputElement).value) }])" />
              <input v-else-if="p.type === 'boolean'" :id="`p-${c.component}-${p.name}`" type="checkbox" :checked="p.value === true" :disabled="draft.allowed_actions.edit != null || busy" @change="edit([{ path: `/components/${c.component}/config/${p.name}`, value: ($event.target as HTMLInputElement).checked }])" />
              <span v-else class="mono">{{ p.value }}</span>
            </dd>
          </template>
        </dl>
        <button v-if="draft.allowed_actions.edit == null && c.component !== 'nginx'" type="button" :disabled="busy" @click="edit([{ path: `/components/${c.component}`, value: null }])">Retirer {{ c.component }}</button>
      </div>
      <div v-if="draft.allowed_actions.edit == null && available.length" class="actions" style="margin-top: 0.75rem">
        <label for="add-comp" class="small">Ajouter un composant du catalogue</label>
        <select id="add-comp" v-model="addSlug">
          <option value="">Choisir…</option>
          <option v-for="c in available" :key="`${c.slug}@${c.version}`" :value="c.slug">{{ c.title }} {{ c.version }} ({{ VERIF[c.verification_level] || 'non testé' }})</option>
        </select>
        <button type="button" :disabled="busy || !addSlug" @click="addComponent">Ajouter</button>
      </div>
    </section>

    <section v-if="pending.length" class="card" aria-labelledby="sug-title" data-testid="suggestions">
      <h2 id="sug-title">Suggestions en attente</h2>
      <p class="small">Rien n'est appliqué tant que vous n'acceptez pas. Une suggestion n'est ni un réglage publié, ni une approbation, ni un ordre.</p>
      <div v-for="s in pending" :key="s.id" class="actions" style="margin: 0.4rem 0">
        <span>{{ s.label }}</span>
        <span class="muted small">{{ s.why }}</span>
        <button type="button" class="primary" :disabled="busy" :data-testid="`accept-${s.id}`" @click="decide(s.id, 'accepted')">Accepter</button>
        <button type="button" :disabled="busy" @click="decide(s.id, 'rejected')">Refuser</button>
      </div>
    </section>

    <section v-if="draft.questions.length" class="card">
      <h2>Questions ouvertes</h2>
      <ul class="small"><li v-for="(q, i) in draft.questions" :key="i">{{ q.text }}</li></ul>
    </section>

    <section v-if="draft.extensions.length" class="card" data-testid="extensions">
      <h2>Propositions d'extension</h2>
      <p class="small">À revoir (contrat, rôle, tests, preuve). Jamais appliquées et jamais exécutées.</p>
      <ul class="small"><li v-for="e in draft.extensions" :key="e.id"><strong>{{ e.subject }}</strong> — {{ e.summary }}</li></ul>
    </section>

    <section class="card" aria-labelledby="assist-title" data-testid="assistant">
      <h2 id="assist-title">Assistant de conception <SimBadge v-if="draft.assistant.simulated" label="Assistant fictif" /></h2>
      <form @submit.prevent="assist">
        <label for="assist-msg" class="small">Décrire un résultat, pas une commande</label>
        <textarea id="assist-msg" v-model="message" rows="3" maxlength="2000" :disabled="draft.allowed_actions.assist != null" data-testid="assist-message" />
        <button type="submit" class="primary" :disabled="busy || draft.allowed_actions.assist != null" style="margin-top: 0.5rem">Proposer</button>
      </form>
      <p v-if="draft.allowed_actions.assist" class="small muted">{{ draft.allowed_actions.assist }}</p>
    </section>

    <section class="card">
      <h2>Provenance</h2>
      <table>
        <thead><tr><th scope="col">Réglage</th><th scope="col">Valeur</th><th scope="col">Origine</th></tr></thead>
        <tbody>
          <tr v-for="p in draft.provenance" :key="p.path">
            <td class="mono small">{{ p.path }}</td><td>{{ p.value }}</td><td>{{ p.source_label }}<span v-if="p.by" class="muted"> · {{ p.by }}</span></td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="card">
      <h2>Soumettre à la revue</h2>
      <ul v-if="draft.problems.length" class="small">
        <li v-for="p in draft.problems" :key="p.path">{{ p.message }}</li>
      </ul>
      <p class="small">La soumission fige cette conception. Elle ne publie rien et ne crée aucun déploiement.</p>
      <button type="button" class="primary" :disabled="busy || draft.allowed_actions.submit != null" data-testid="submit-draft" @click="submit">Soumettre {{ draft.version }}</button>
      <p v-if="draft.allowed_actions.submit" class="small muted">{{ draft.allowed_actions.submit }}</p>
    </section>
  </div>
</template>
