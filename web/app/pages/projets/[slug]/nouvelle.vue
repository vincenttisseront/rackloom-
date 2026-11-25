<script setup lang="ts">
// Étapes 1 « Décrire le besoin » et 2 « Vérifier et soumettre ». Le formulaire est généré depuis le
// contrat d'entrées du service : aucun champ d'infrastructure (invariant 1 et 2).
const route = useRoute()
const api = useApi()
const slug = computed(() => String(route.params.slug))

type Field<T> = { value: T; source: 'user' | 'suggestion' | null }
const services = ref<any[]>([])
const project = ref<any>(null)
const change = ref<any>(null)
const step = ref<1 | 2>(1)
const loading = ref(true)
const saving = ref(false)
const error = ref<string | null>(null)
const fieldErrors = ref<Record<string, string>>({})
const usedChat = ref(false)
const prodDialog = ref<HTMLDialogElement | null>(null)

const form = reactive({
  template: { value: null, source: null } as Field<string | null>,
  environment: { value: null, source: null } as Field<string | null>,
  confirm_production: false,
  quantity: { value: null, source: null } as Field<number | null>,
  size_tier: { value: null, source: null } as Field<string | null>,
  usage: { value: '', source: null } as Field<string>,
  lifetime_until: { value: '', source: null } as Field<string>,
})

const service = computed(() => services.value.find(s => s.slug === form.template.value) || null)
const environments = computed(() => service.value ? [...service.value.environments, ...(form.confirm_production ? service.value.production : [])] : [])
const env = computed(() => {
  if (!service.value) return null
  if (form.environment.value) return environments.value.find((e: any) => e.slug === form.environment.value) || null
  return service.value.environments.length === 1 ? service.value.environments[0] : null
})
const isModify = computed(() => change.value?.kind === 'modify')
const revision = computed(() => change.value?.revision || null)
const canSubmit = computed(() => change.value?.allowed_actions?.some((a: any) => a.action === 'submit'))
const maxDate = computed(() => env.value?.lifetime?.max_days ? isoDay(env.value.lifetime.max_days) : undefined)
const choices = computed(() => [form.template, form.environment, form.quantity, form.size_tier, form.usage, form.lifetime_until].filter(f => f.source === 'user').length)

function set<T>(f: Field<T>, value: T) {
  f.value = value
  f.source = 'user'
}

function fromNeed(need: any, provenance: Record<string, { source: string }> = {}) {
  const src = (p: string) => (provenance[p]?.source === 'suggestion' ? 'suggestion' : 'user') as 'user' | 'suggestion'
  form.template = { value: need.service?.template ?? null, source: need.service?.template ? src('/service/template') : null }
  form.environment = { value: need.environment ?? null, source: need.environment ? src('/environment') : null }
  form.confirm_production = !!need.confirm_production
  for (const k of ['quantity', 'size_tier', 'usage', 'lifetime_until'] as const) {
    const has = need.inputs && k in need.inputs
    ;(form as any)[k] = { value: has ? need.inputs[k] : k === 'usage' || k === 'lifetime_until' ? '' : null, source: has ? src(`/inputs/${k}`) : null }
  }
}

function buildNeed(partial = false) {
  const inputs: Record<string, any> = {}
  const provenance: Record<string, { source: string }> = {}
  for (const k of ['quantity', 'size_tier', 'usage', 'lifetime_until'] as const) {
    const f = (form as any)[k] as Field<any>
    if (f.source && f.value !== null && f.value !== '') {
      inputs[k] = f.value
      provenance[`/inputs/${k}`] = { source: f.source }
    }
  }
  if (form.template.source) provenance['/service/template'] = { source: form.template.source }
  if (form.environment.source && form.environment.value) provenance['/environment'] = { source: form.environment.source }
  const need = {
    schema: 'rackloom.need/v1',
    service: { template: form.template.value },
    environment: form.environment.value,
    confirm_production: form.confirm_production,
    inputs,
  }
  return partial ? need : { need, provenance }
}

function applySuggestion(p: { path: string; value: any }) {
  const map: Record<string, Field<any>> = {
    '/service/template': form.template, '/environment': form.environment, '/inputs/quantity': form.quantity,
    '/inputs/size_tier': form.size_tier, '/inputs/usage': form.usage, '/inputs/lifetime_until': form.lifetime_until,
  }
  const f = map[p.path]
  if (f) { f.value = p.value; f.source = 'suggestion' }
}

function confirmSuggestion(f: Field<any>) {
  f.source = 'user'
}

function askProduction() {
  prodDialog.value?.showModal()
}
function confirmProduction() {
  const prod = service.value?.production?.[0]
  if (prod) {
    form.confirm_production = true
    set(form.environment, prod.slug)
  }
  prodDialog.value?.close()
}
function cancelProduction() {
  form.confirm_production = false
  if (form.environment.value && service.value?.production?.some((e: any) => e.slug === form.environment.value)) {
    form.environment = { value: null, source: null }
  }
}

async function load() {
  loading.value = true
  try {
    const [svc, projects] = await Promise.all([api.get(`/projects/${slug.value}/services`), api.get('/projects')])
    services.value = svc
    project.value = projects.find((p: any) => p.slug === slug.value) || null
    if (route.query.change) {
      change.value = await api.get(`/changes/${route.query.change}`)
      if (change.value.need) fromNeed(change.value.need, change.value.need_provenance || {})
      if (route.query.etape === '2' && change.value.revision) step.value = 2
    }
  } catch (e) {
    error.value = errorText(e)
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function save() {
  saving.value = true
  error.value = null
  fieldErrors.value = {}
  try {
    if (!change.value) {
      change.value = await api.post(`/projects/${slug.value}/changes`, { title: service.value?.title || 'Nouvelle demande' })
      await navigateTo({ query: { change: change.value.id } }, { replace: true })
    }
    const res = await api.post(`/changes/${change.value.id}/versions`, {
      ...buildNeed(),
      base_version_no: change.value.current_version_no,
      source: usedChat.value ? 'chat' : 'webui',
    })
    change.value = res.change
    step.value = 2
    window.scrollTo({ top: 0 })
  } catch (e) {
    if (e instanceof ApiError && e.field) fieldErrors.value[e.field] = e.message
    error.value = errorText(e)
  } finally {
    saving.value = false
  }
}

async function submit() {
  saving.value = true
  error.value = null
  try {
    await api.post(`/changes/${change.value.id}/submit`, { revision_no: change.value.current_revision_no })
    await navigateTo(`/demandes/${change.value.id}`)
  } catch (e) {
    error.value = errorText(e)
    change.value = await api.get(`/changes/${change.value.id}`).catch(() => change.value)
  } finally {
    saving.value = false
  }
}

const inputs = computed(() => service.value?.inputs || {})
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <div class="crumbs"><NuxtLink :to="`/projets/${slug}`">{{ project?.name || slug }}</NuxtLink> ›
          {{ change ? change.ref : 'Nouvelle demande' }}</div>
        <h1>{{ isModify ? 'Modifier ce déploiement' : change ? `Demande ${change.ref}` : 'Nouvelle demande' }}</h1>
      </div>
    </div>
    <StepIndicator :current="step" />
    <div v-if="error" class="banner bad" role="alert" data-testid="error">{{ error }}</div>
    <p v-if="loading" class="empty">Chargement…</p>

    <!-- Étape 1 -->
    <div v-else-if="step === 1" class="grid-2">
      <AssistantPanel :project="slug" :need="() => buildNeed(true)" @apply="applySuggestion" @used="usedChat = true" />

      <form class="card" aria-labelledby="need-title" data-testid="need-form" novalidate @submit.prevent="save">
        <h2 id="need-title">Votre besoin</h2>
        <p class="small muted">Décrivez le résultat attendu. Rackloom choisit l'emplacement, le réseau, l'image et le dimensionnement.</p>

        <fieldset class="field" :disabled="isModify" style="border: none; padding: 0; margin: 0 0 1rem">
          <legend><strong>Service</strong></legend>
          <p v-if="!services.length" class="banner warn">Aucun service n'est disponible pour ce projet.</p>
          <div v-for="s in services" :key="s.slug" class="actions" style="margin: 0.35rem 0">
            <input :id="`svc-${s.slug}`" type="radio" name="service" :value="s.slug" :checked="form.template.value === s.slug" @change="set(form.template, s.slug)" />
            <label :for="`svc-${s.slug}`" style="font-weight: 500; margin: 0">
              <strong>{{ s.title }}</strong> <span class="muted small">— {{ s.description }} (version {{ s.version }})</span>
            </label>
          </div>
          <div v-if="form.template.source" class="actions small">
            <ProvenanceBadge :source="form.template.source" />
            <button v-if="form.template.source === 'suggestion'" type="button" @click="confirmSuggestion(form.template)">Confirmer ce service</button>
          </div>
          <p v-if="fieldErrors['/service/template']" class="field-error">{{ fieldErrors['/service/template'] }}</p>
        </fieldset>

        <template v-if="service">
          <div v-if="service.environments.length > 1 || form.confirm_production" class="field">
            <div class="field-head">
              <label for="f-env">Environnement</label>
              <ProvenanceBadge v-if="form.environment.source" :source="form.environment.source" />
            </div>
            <select id="f-env" :value="form.environment.value || ''" :disabled="isModify" @change="set(form.environment, ($event.target as HTMLSelectElement).value || null)">
              <option value="">Laisser Rackloom choisir (jamais la production)</option>
              <option v-for="e in environments" :key="e.slug" :value="e.slug">{{ e.name }}{{ e.is_production ? ' (production)' : '' }}</option>
            </select>
            <button v-if="form.environment.source === 'suggestion'" type="button" class="small" @click="confirmSuggestion(form.environment)">Confirmer cet environnement</button>
          </div>
          <p v-else class="small">Environnement : <strong>{{ env?.name }}</strong> <ProvenanceBadge source="environment_default" /></p>
          <p v-if="form.confirm_production" class="banner warn small" data-testid="production-banner">
            Déploiement en production demandé explicitement. Circuit d'approbation de la production : approbateur distinct obligatoire.
            <button type="button" class="link" @click="cancelProduction">Annuler la demande de production</button>
          </p>

          <div v-if="inputs.quantity" class="field">
            <div class="field-head">
              <label for="f-quantity">{{ inputs.quantity.label }}</label>
              <ProvenanceBadge :source="form.quantity.source || 'template_default'" />
            </div>
            <input id="f-quantity" type="number" :min="inputs.quantity.min" :max="inputs.quantity.max" :value="form.quantity.value ?? inputs.quantity.default" @input="set(form.quantity, Number(($event.target as HTMLInputElement).value))" />
            <p class="hint">Entre {{ inputs.quantity.min }} et {{ inputs.quantity.max }}.</p>
            <p v-if="fieldErrors['/inputs/quantity']" class="field-error">{{ fieldErrors['/inputs/quantity'] }}</p>
          </div>

          <div v-if="inputs.size_tier && env && env.size_tiers.length > 1" class="field">
            <div class="field-head">
              <label for="f-size">{{ inputs.size_tier.label }}</label>
              <ProvenanceBadge :source="form.size_tier.source || 'template_default'" />
            </div>
            <select id="f-size" :value="form.size_tier.value || env.default_size_tier" @change="set(form.size_tier, ($event.target as HTMLSelectElement).value)">
              <option v-for="t in env.size_tiers" :key="t.value" :value="t.value">{{ t.label }}{{ t.value === env.default_size_tier ? ' (défaut)' : '' }}</option>
            </select>
            <p v-if="fieldErrors['/inputs/size_tier']" class="field-error">{{ fieldErrors['/inputs/size_tier'] }}</p>
          </div>

          <div v-if="inputs.usage" class="field">
            <div class="field-head">
              <label for="f-usage">{{ inputs.usage.label }} <span class="muted small">(facultatif)</span></label>
              <ProvenanceBadge v-if="form.usage.source" :source="form.usage.source" />
            </div>
            <input id="f-usage" type="text" :maxlength="inputs.usage.max_length" :value="form.usage.value" @input="set(form.usage, ($event.target as HTMLInputElement).value)" />
          </div>

          <div v-if="inputs.lifetime_until" class="field">
            <div class="field-head">
              <label for="f-until">{{ inputs.lifetime_until.label }}<span v-if="env?.lifetime?.required"> (obligatoire)</span></label>
              <ProvenanceBadge v-if="form.lifetime_until.source" :source="form.lifetime_until.source" />
            </div>
            <input id="f-until" type="date" :min="isoDay(1)" :max="maxDate" :value="form.lifetime_until.value" :aria-describedby="'f-until-hint'" @input="set(form.lifetime_until, ($event.target as HTMLInputElement).value)" />
            <p id="f-until-hint" class="hint">
              <template v-if="env?.lifetime?.max_days">{{ env.lifetime.max_days }} jours au plus. </template>
              Résolue et affichée, non appliquée au lot 1 <span class="badge neutral">Non appliqué</span>
            </p>
            <p v-if="fieldErrors['/inputs/lifetime_until']" class="field-error">{{ fieldErrors['/inputs/lifetime_until'] }}</p>
          </div>

          <p v-if="service.production?.length && !form.confirm_production && !isModify" class="small">
            <button type="button" class="link" data-testid="ask-production" @click="askProduction">Demander un déploiement en production</button>
          </p>
        </template>

        <div class="actions" style="margin-top: 1rem">
          <button type="submit" class="primary" :disabled="saving || !form.template.value" data-testid="continue">Continuer</button>
          <span class="small muted">{{ choices }} choix de votre part</span>
        </div>
      </form>
    </div>

    <!-- Étape 2 -->
    <div v-else-if="revision" data-testid="review">
      <section class="card" aria-labelledby="summary-title">
        <h2 id="summary-title">Récapitulatif</h2>
        <template v-if="revision.outcome === 'blocked'">
          <div v-for="b in revision.blocks" :key="b.code" class="banner bad" role="alert" data-testid="block">{{ b.message }}</div>
        </template>
        <template v-else-if="revision.outcome === 'incomplete'">
          <div class="banner warn" role="alert">
            Précisions requises :
            <ul><li v-for="m in revision.missing" :key="m.field">{{ m.text }}</li></ul>
          </div>
        </template>
        <template v-else>
          <p class="big" data-testid="summary-headline"><strong>{{ revision.summary.headline }}</strong></p>
          <dl class="kv">
            <dt>Création</dt><dd>{{ revision.summary.creates.join(', ') || '—' }}</dd>
            <dt v-if="revision.summary.keeps?.length">Conservé</dt><dd v-if="revision.summary.keeps?.length">{{ revision.summary.keeps.join(', ') }}</dd>
            <dt>Vérifications</dt><dd>{{ revision.summary.verifications }}</dd>
            <dt>Approbation</dt><dd>{{ revision.summary.approval }}</dd>
            <dt>Service</dt><dd>{{ revision.template?.title }} {{ revision.template?.version }}</dd>
          </dl>
        </template>
      </section>

      <section v-if="revision.outcome !== 'blocked'" class="card" aria-labelledby="risks-title">
        <h2 id="risks-title">Risques et limites</h2>
        <ul>
          <li v-if="revision.summary.production"><span class="badge warn">Production</span> Déploiement en production, circuit d'approbation de la production.</li>
          <li v-if="revision.summary.replaces?.length" data-testid="risk-destructive">
            <span class="badge bad">Bloquant</span> Changer le niveau remplacerait {{ revision.summary.replaces.join(', ') }}
            (suppression puis recréation). Rackloom ne supprime rien au lot 1 : le plan sera bloqué et ne pourra pas être approuvé.
          </li>
          <li v-if="revision.summary.removes?.length" data-testid="risk-destructive">
            <span class="badge bad">Bloquant</span> Réduire la quantité supprimerait {{ revision.summary.removes.join(', ') }}.
            Rackloom ne supprime rien au lot 1 : le plan sera bloqué et ne pourra pas être approuvé.
          </li>
          <li>Approbation requise : {{ revision.summary.approval }}.</li>
          <li v-for="l in revision.summary.preview_limits" :key="l">{{ l }}</li>
          <li>Ports ouverts, sauvegarde et durée de vie : résolus et affichés, non appliqués <span class="badge neutral">Non appliqué</span></li>
          <li>Étapes simulées : inventaire vCenter, personnalisation invitée, attribution d'adresse <SimBadge /></li>
          <li>Les ressources créées sont des ressources de labo locales, pas des VM <span class="badge neutral">Ressource de labo</span></li>
        </ul>
      </section>

      <PlanDetails v-if="revision.details" :details="revision.details" />

      <div class="actions">
        <button type="button" @click="step = 1">Modifier le besoin</button>
        <button v-if="canSubmit" type="button" class="primary" :disabled="saving" data-testid="submit" @click="submit">Soumettre</button>
        <span v-else-if="revision.outcome === 'resolved'" class="small muted">Soumission indisponible dans l'état actuel.</span>
      </div>
    </div>

    <dialog ref="prodDialog" aria-labelledby="prod-title">
      <h2 id="prod-title">Demander un déploiement en production ?</h2>
      <p>La production n'est jamais choisie par défaut. Elle suit son propre circuit : approbateur distinct obligatoire et politiques de la production.</p>
      <div class="actions">
        <button type="button" @click="prodDialog?.close()">Annuler</button>
        <button type="button" class="primary" data-testid="confirm-production" @click="confirmProduction">Je demande la production</button>
      </div>
    </dialog>
  </div>
</template>
