<script setup lang="ts">
// Administration › Templates (sans atelier) : publier n'est pas déployer. Aucune action ne crée de job.
const api = useApi()
const templates = ref<any[]>([])
const components = ref<any[]>([])
const selected = ref<any>(null)
const impact = ref<any>(null)
const reason = ref('')
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const STATUS: Record<string, string> = {
  validated: 'Validée', in_review: 'En revue', returned: 'Renvoyée', published: 'Publiée',
  deprecated: 'Dépréciée', withdrawn: 'Retirée', draft: 'Brouillon', lab_tested: 'Testée en labo',
}
const ACTIONS: Record<string, string> = { publish: 'Publier', return: 'Renvoyer à l\'auteur', deprecate: 'Déprécier', withdraw: 'Retirer' }
const VERIF: Record<string, string> = { not_tested: 'non testé', tested_locally: 'testé localement', lab_validated: 'validé en laboratoire' }

async function load() {
  try {
    templates.value = await api.get('/admin/templates')
    components.value = await api.get('/admin/components')
  } catch (e) {
    error.value = errorText(e)
  }
}
onMounted(load)

async function open(slug: string, version: string) {
  error.value = null
  notice.value = null
  reason.value = ''
  selected.value = await api.get(`/admin/templates/${slug}/versions/${version}`)
  impact.value = await api.get(`/admin/templates/${slug}/versions/${version}/impact`)
}

async function act(action: string) {
  if (!selected.value) return
  busy.value = true
  error.value = null
  try {
    await api.post(`/admin/templates/${selected.value.slug}/versions/${selected.value.version}/${action}`, {
      reason: reason.value, digest: selected.value.digest,
    })
    notice.value = `${ACTIONS[action]} : fait. Aucun job créé, aucun déploiement modifié.`
    await load()
    await open(selected.value.slug, selected.value.version)
    notice.value = `${ACTIONS[action]} : fait. Aucun job créé, aucun déploiement modifié.`
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>Administration › Templates</h1>
        <p class="muted small">Versions immuables du catalogue. Publier rend une version disponible pour les nouvelles demandes ; cela ne déploie rien.</p>
      </div>
    </div>
    <div v-if="error" class="banner bad" role="alert" data-testid="error">{{ error }}</div>
    <div v-if="notice" class="banner ok" role="status">{{ notice }}</div>
    <div class="grid-main">
      <div>
        <section class="card" aria-labelledby="tpl-title">
          <h2 id="tpl-title">Templates</h2>
          <div v-for="t in templates" :key="t.slug">
            <h3>{{ t.title }} <span class="muted small mono">{{ t.slug }}</span></h3>
            <table>
              <thead><tr><th scope="col">Version</th><th scope="col">Statut</th><th scope="col">Empreinte</th><th scope="col"></th></tr></thead>
              <tbody>
                <tr v-for="v in t.versions" :key="v.version">
                  <td>{{ v.version }}<span v-if="v.version === t.latest_published" class="badge ok" style="margin-left: 0.4rem">dernière publiée</span></td>
                  <td>{{ STATUS[v.status] || v.status }}</td>
                  <td class="mono small">{{ v.digest.slice(0, 12) }}…</td>
                  <td><button type="button" :data-testid="`open-${t.slug}-${v.version}`" @click="open(t.slug, v.version)">Ouvrir</button></td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section v-if="selected" class="card" aria-labelledby="ver-title" data-testid="template-version">
          <h2 id="ver-title">{{ selected.contract.title }} {{ selected.version }} <span class="badge info">{{ STATUS[selected.status] }}</span></h2>
          <p class="small">{{ selected.contract.description }}</p>
          <p v-if="selected.separation_notice" class="banner warn" data-testid="separation-notice">{{ selected.separation_notice }}</p>
          <template v-if="selected.review">
            <h3>Revue</h3>
            <p class="small" data-testid="review-origin">{{ selected.review.origin_label }}. Auteur : {{ selected.review.author || '—' }}.</p>
            <ul class="small" data-testid="review-changes">
              <li v-for="c in selected.review.changes_from_base" :key="c">{{ c }}</li>
            </ul>
            <p v-if="!selected.review.changes_from_base.length" class="small muted">Aucun écart avec la base.</p>
            <h3 v-if="selected.review.extensions.length">Propositions d'extension (non appliquées)</h3>
            <ul class="small">
              <li v-for="e in selected.review.extensions" :key="e.subject">{{ e.subject }} — {{ e.summary }}</li>
            </ul>
            <p class="banner info small">{{ selected.review.notice }}</p>
          </template>
          <h3>Entrées proposées au demandeur</h3>
          <ul class="small">
            <li v-for="(i, k) in selected.contract.inputs" :key="k">{{ i.label }} <span class="muted mono">({{ k }}, {{ i.type }})</span></li>
          </ul>
          <h3>Composants</h3>
          <ul class="small">
            <li v-for="c in selected.components" :key="c.slug">{{ c.title }} {{ c.version }} — {{ STATUS[c.status] || c.status }} — vérification : {{ VERIF[c.verification_level] || c.verification_level }}<template v-if="c.evidence"> ({{ c.evidence.count }} preuve(s))</template></li>
          </ul>
          <h3>Contrôles de santé</h3>
          <ul class="small"><li v-for="(h, i) in selected.contract.health_checks" :key="i">{{ h.type }} <span class="muted mono">{{ h.package || h.unit || `:${h.port}${h.path}` }}</span></li></ul>
          <h3>Environnements</h3>
          <p class="small">{{ Object.keys(selected.contract.environments).join(', ') }}</p>
          <h3>Historique</h3>
          <ul class="small">
            <li v-for="(h, i) in selected.history" :key="i">{{ dateTime(h.at) }} · {{ h.version }} · {{ h.action }} — {{ h.reason }}</li>
          </ul>
          <template v-if="impact">
            <h3>Aperçu d'impact</h3>
            <p class="small">Projets concernés : {{ impact.projects.join(', ') || 'aucun' }} · plans en attente sur cette version : {{ impact.plans_pending_on_version }}</p>
            <ul class="small"><li v-for="d in impact.deployments" :key="d.id">{{ d.project }} / {{ d.environment }} : épinglé en {{ d.pinned_version }}</li></ul>
            <p class="banner info small">{{ impact.note }}</p>
          </template>
          <div v-if="selected.allowed_actions.length" class="field">
            <label for="pub-reason">Motif (obligatoire)</label>
            <input id="pub-reason" v-model="reason" type="text" maxlength="500" />
            <div class="actions" style="margin-top: 0.5rem">
              <button v-for="a in selected.allowed_actions" :key="a" type="button" :class="a === 'publish' ? 'primary' : 'danger'" :disabled="busy || !reason.trim()" :data-testid="`action-${a}`" @click="act(a)">
                {{ ACTIONS[a] || a }}
              </button>
            </div>
          </div>
        </section>
      </div>
      <section class="card" aria-labelledby="comp-title">
        <h2 id="comp-title">Composants du registre</h2>
        <ul class="small">
          <li v-for="c in components" :key="`${c.slug}-${c.version}`">{{ c.title }} {{ c.version }} <span class="muted">({{ STATUS[c.status] || c.status }}, {{ VERIF[c.verification_level] || c.verification_level }})</span></li>
        </ul>
      </section>
    </div>
  </div>
</template>
