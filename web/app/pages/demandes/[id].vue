<script setup lang="ts">
// Étape 3 « Suivre le résultat » et écran « Plan et approbation ». Les boutons viennent de allowed_actions,
// calculé par le serveur et revérifié à l'appel : masquer un bouton n'est pas un contrôle.
import type { RackloomEvent } from '~/composables/useEventStream'

const route = useRoute()
const api = useApi()
const id = computed(() => String(route.params.id))

const change = ref<any>(null)
const jobs = ref<any[]>([])
const events = ref<RackloomEvent[]>([])
const deployment = ref<any>(null)
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const comment = ref('')
const explain = ref<string | null>(null)
const diagnosis = ref<any | null>(null)
const contextPreview = ref<any | null>(null)
const excludedExtracts = ref<number[]>([])

const actions = computed<string[]>(() => (change.value?.allowed_actions || []).map((a: any) => a.action))
const label = (a: string) => change.value?.allowed_actions?.find((x: any) => x.action === a)?.label || a
const can = (a: string) => actions.value.includes(a)
const service = computed(() => change.value?.deployment || null)
const finished = computed(() => ['succeeded', 'partially_succeeded', 'failed', 'rejected', 'cancelled'].includes(change.value?.state))

let refreshTimer: ReturnType<typeof setTimeout> | null = null
function scheduleRefresh() {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = setTimeout(refresh, 400)
}

async function refresh() {
  try {
    const [c, j] = await Promise.all([api.get(`/changes/${id.value}`), api.get(`/changes/${id.value}/jobs`)])
    change.value = c
    jobs.value = j
    deployment.value = c.deployment_id ? await api.get(`/deployments/${c.deployment_id}`) : null
  } catch (e) {
    error.value = errorText(e)
  }
}

const stream = useEventStream(`/changes/${id.value}/events/stream`, (e) => {
  if (!events.value.some(x => x.id === e.id)) events.value.push(e)
  scheduleRefresh()
})

onMounted(async () => {
  await refresh()
  try {
    const past = await api.get<RackloomEvent[]>(`/changes/${id.value}/events`)
    events.value = past
    stream.setAfter(past.length ? past[past.length - 1]!.id : 0)
  } catch {
    // le flux reprend depuis le début
  }
  stream.start()
})
onBeforeUnmount(() => {
  stream.stop()
  if (refreshTimer) clearTimeout(refreshTimer)
})

async function run(fn: () => Promise<any>, ok?: string) {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    const res = await fn()
    if (ok) notice.value = ok
    await refresh()
    return res
  } catch (e) {
    error.value = errorText(e)
    await refresh()
  } finally {
    busy.value = false
  }
}

const decide = (decision: 'approved' | 'rejected') => run(() => api.post(`/changes/${id.value}/approvals`, {
  plan_id: change.value.plan.id, bundle_digest: change.value.plan.bundle_digest, decision, comment: comment.value || undefined,
}), decision === 'approved' ? 'Plan approuvé : exécution du plan approuvé.' : 'Plan refusé.')

const decideRetry = (decision: 'approved' | 'rejected') => run(() => api.post(`/changes/${id.value}/approvals`, {
  retry_id: change.value.pending_retry.id, bundle_digest: change.value.pending_retry.bundle_digest, decision, comment: comment.value || undefined,
}), decision === 'approved' ? 'Reprise approuvée.' : 'Reprise refusée.')

const cancelChange = () => confirm('Annuler cette demande ?') && run(() => api.post(`/changes/${id.value}/cancel`), 'Demande annulée.')
const replan = () => run(() => api.post(`/changes/${id.value}/replan`), 'Nouvelle planification demandée.')
const requestRetry = () => run(() => api.post(`/changes/${id.value}/retries`, { step: 'ansible_configure' }), 'Reprise de la configuration demandée : approbation requise.')
const cancelJob = (jobId: string) => run(() => api.post(`/jobs/${jobId}/cancel`), "Annulation demandée. Elle n'est pas garantie : l'état réel s'affiche ci-dessous.")

async function duplicate() {
  const res = await run(() => api.post(`/changes/${id.value}/duplicate`))
  if (res?.change) await navigateTo(`/projets/${res.change.project.slug}/nouvelle?change=${res.change.id}`)
}
async function modify() {
  const res = await run(() => api.post(`/deployments/${change.value.deployment_id}/changes`))
  if (res?.change) await navigateTo(`/projets/${res.change.project.slug}/nouvelle?change=${res.change.id}`)
}

const explainConvId = ref<string | null>(null)

async function loadContextPreview() {
  diagnosis.value = null
  explain.value = null
  try {
    if (!explainConvId.value) {
      const conv = await api.post(`/projects/${change.value.project.slug}/conversations`, {})
      explainConvId.value = conv.id
    }
    const lastJob = jobs.value[jobs.value.length - 1]
    contextPreview.value = await api.post(`/conversations/${explainConvId.value}/context-preview`, {
      mode: 'explain',
      job_id: lastJob?.id,
      exclude_extract_ids: excludedExtracts.value,
    })
  } catch (e) {
    explain.value = errorText(e)
  }
}

async function explainJob() {
  explain.value = 'Analyse en cours…'
  diagnosis.value = null
  try {
    if (!explainConvId.value) await loadContextPreview()
    const lastJob = jobs.value[jobs.value.length - 1]
    const res = await api.post(`/conversations/${explainConvId.value}/messages`, {
      message: 'Explique-moi ce job',
      mode: 'explain',
      job_id: lastJob?.id,
      exclude_extract_ids: excludedExtracts.value,
    })
    explain.value = res.turn?.reply || res.notice || "L'assistant n'a pas pu répondre."
    diagnosis.value = res.diagnosis || null
    if (res.status === 'unavailable') {
      explain.value = res.notice || explain.value
    }
  } catch (e) {
    explain.value = errorText(e)
  }
}

function toggleExtract(id: number) {
  if (excludedExtracts.value.includes(id)) {
    excludedExtracts.value = excludedExtracts.value.filter(x => x !== id)
  } else {
    excludedExtracts.value = [...excludedExtracts.value, id]
  }
}

const serviceCard = computed(() => deployment.value?.service_card || service.value?.service_card || null)
</script>

<template>
  <div v-if="change">
    <div class="page-head">
      <div>
        <div class="crumbs"><NuxtLink :to="`/projets/${change.project.slug}`">{{ change.project.name }}</NuxtLink> › {{ change.ref }}</div>
        <h1>{{ change.title }} <StatePill :state="change.state" :label="change.state_label" :service-status="service?.service_status" /></h1>
        <p class="small muted">
          {{ change.kind === 'modify' ? 'Modification de déploiement' : 'Création' }} ·
          {{ change.environment ? `${change.project.name} / ${change.environment.name}` : '' }}
          <span v-if="change.environment?.is_production" class="badge warn">Production</span>
          · demandé par {{ change.created_by }} le {{ dateTime(change.created_at) }}
        </p>
      </div>
      <div class="actions">
        <NuxtLink v-if="can('edit')" class="button" :to="`/projets/${change.project.slug}/nouvelle?change=${change.id}`">{{ label('edit') }}</NuxtLink>
        <NuxtLink v-if="can('submit')" class="button primary" :to="`/projets/${change.project.slug}/nouvelle?change=${change.id}&etape=2`">{{ label('submit') }}</NuxtLink>
        <button v-if="can('replan')" type="button" :disabled="busy" @click="replan">{{ label('replan') }}</button>
        <button v-if="can('duplicate')" type="button" :disabled="busy" @click="duplicate">{{ label('duplicate') }}</button>
        <button v-if="can('cancel')" type="button" class="danger" :disabled="busy" @click="cancelChange">{{ label('cancel') }}</button>
      </div>
    </div>

    <StepIndicator :current="3" />
    <div v-if="error" class="banner bad" role="alert" data-testid="error">{{ error }}</div>
    <div v-if="notice" class="banner info" role="status">{{ notice }}</div>
    <div v-if="stream.ended.value" class="banner warn" role="status">Session terminée : le suivi en direct est interrompu. Reconnectez-vous.</div>
    <div v-if="change.environment_freeze" class="banner warn" role="status" data-testid="freeze-banner">
      <strong>Environnement {{ change.environment?.name }} gelé</strong> depuis le {{ dateTime(change.environment_freeze.frozen_at) }}
      par {{ change.environment_freeze.frozen_by }} : « {{ change.environment_freeze.reason }} ».
      Aucun job d'écriture ne démarre pendant le gel, même déjà approuvé ; un job déjà envoyé au fournisseur continue et son état réel s'affiche ci-dessous.
      La lecture et le diagnostic restent disponibles.
    </div>

    <!-- F57 : fiche service — état observé + contrôles de santé, jamais le seul succès d'un job. -->
    <div v-if="serviceCard" data-testid="service-result">
      <ServiceCard :card="serviceCard" />
      <p v-if="change.state === 'succeeded' && serviceCard.status === 'ready'" class="small muted" style="margin-top: 0.35rem" data-testid="change-success-note">
        Succès — service prêt
      </p>
      <p v-else-if="change.state === 'partially_succeeded'" class="small muted" style="margin-top: 0.35rem">
        Demande en succès partiel : le service n'est pas prêt.
      </p>
    </div>
    <div v-else-if="service" class="result" :class="SERVICE_TONE[service.service_status] || 'neutral'" data-testid="service-result">
      <div class="big">Service : {{ service.service_status_label }}</div>
      <div class="small">{{ service.service_reason || 'Pas encore vérifié.' }}</div>
    </div>
    <div
      v-if="change.reason && !service"
      class="banner"
      :class="change.state === 'plan_failed' ? 'bad' : 'info'"
      role="status"
      data-testid="change-reason"
    >{{ change.reason }}</div>

    <div class="grid-main">
      <div>
        <PlanPanel v-if="change.plan" :plan="change.plan">
          <div v-if="can('approve') || can('reject')" class="card" style="margin-top: 1rem; background: var(--surface-2)" data-testid="approval">
            <h3>Votre décision</h3>
            <p class="small">Vous approuvez exactement ce plan et cette empreinte. Une nouvelle version du besoin révoquerait l'approbation.</p>
            <p v-if="change.environment_freeze && !can('approve')" class="small field-error" data-testid="approval-frozen">Approbation suspendue pendant le gel de l'environnement ; le refus reste possible.</p>
            <label for="approval-comment">Commentaire <span class="muted small">(obligatoire pour un refus)</span></label>
            <textarea id="approval-comment" v-model="comment" maxlength="1000" />
            <div class="actions" style="margin-top: 0.5rem">
              <button v-if="can('approve')" type="button" class="primary" :disabled="busy" data-testid="approve" @click="decide('approved')">{{ label('approve') }}</button>
              <button v-if="can('reject')" type="button" class="danger" :disabled="busy || !comment.trim()" @click="decide('rejected')">{{ label('reject') }}</button>
            </div>
          </div>
          <p v-else-if="change.state === 'plan_ready'" class="banner info small" data-testid="awaiting-approval">
            <template v-if="change.environment_freeze">Approbation suspendue : l'environnement est gelé.</template>
            <template v-else>En attente d'approbation par {{ change.revision?.summary?.approval }}.</template>
          </p>
        </PlanPanel>

        <section v-if="change.pending_retry" class="card" data-testid="pending-retry">
          <h2>Reprise de la configuration demandée</h2>
          <p class="small">Hôtes concernés : {{ change.pending_retry.host_ids.length }} · demandée le {{ dateTime(change.pending_retry.requested_at) }}</p>
          <template v-if="can('approve_retry') || can('reject_retry')">
            <p v-if="change.environment_freeze && !can('approve_retry')" class="small field-error">Approbation suspendue pendant le gel de l'environnement ; le refus reste possible.</p>
            <label for="retry-comment">Commentaire</label>
            <textarea id="retry-comment" v-model="comment" maxlength="1000" />
            <div class="actions" style="margin-top: 0.5rem">
              <button v-if="can('approve_retry')" type="button" class="primary" :disabled="busy" @click="decideRetry('approved')">{{ label('approve_retry') }}</button>
              <button v-if="can('reject_retry')" type="button" class="danger" :disabled="busy" @click="decideRetry('rejected')">{{ label('reject_retry') }}</button>
            </div>
          </template>
          <p v-else class="small muted">{{ change.environment_freeze ? "Approbation suspendue : l'environnement est gelé." : "En attente d'un approbateur." }}</p>
        </section>

        <div v-if="can('request_retry')" class="banner warn">
          VM créée, configuration incomplète.
          <button type="button" class="primary" :disabled="busy" style="margin-left: 0.5rem" @click="requestRetry">{{ label('request_retry') }}</button>
        </div>

        <!-- F05 : capacité et provenance restent visibles une fois le plan prêt (plan.details). -->
        <PlanDetails
          v-if="change.plan?.details || change.revision?.details"
          :details="change.plan?.details || change.revision.details"
        />

        <section v-if="deployment" class="card" aria-labelledby="hosts-title" data-testid="hosts">
          <div class="field-head">
            <h2 id="hosts-title">Résultat par machine</h2>
            <button v-if="deployment.allowed_actions?.some((a: any) => a.action === 'modify')" type="button" :disabled="busy" data-testid="modify" @click="modify">Modifier ce déploiement</button>
          </div>
          <p v-if="!deployment.hosts.length" class="empty">Aucune machine enrôlée pour l'instant.</p>
          <table v-else>
            <thead><tr><th scope="col">Machine</th><th scope="col">Déploiement</th><th scope="col">Santé</th></tr></thead>
            <tbody>
              <tr v-for="h in deployment.hosts" :key="h.id">
                <td><NuxtLink :to="`/machines/${h.id}`">{{ h.name }}</NuxtLink></td>
                <td>{{ h.deployment_state_label || h.deployment_state }}</td>
                <td><span class="badge" :class="h.health.state === 'healthy' ? 'ok' : h.health.state === 'error' ? 'bad' : 'warn'">{{ h.health.label }}</span></td>
              </tr>
            </tbody>
          </table>
          <div v-if="deployment.service_checks?.length" style="margin-top: 0.75rem">
            <h3>Contrôles de santé</h3>
            <ul class="small">
              <template v-for="hc in deployment.service_checks" :key="hc.host">
                <li v-for="c in hc.checks" :key="`${hc.host}-${c.type}`">
                  <span class="badge" :class="c.ok ? 'ok' : 'bad'">{{ c.ok ? 'OK' : 'KO' }}</span> {{ hc.host }} · {{ c.type }} : {{ c.evidence }}
                </li>
                <li v-if="hc.reason && !hc.checks.length">{{ hc.host }} : {{ hc.reason }}</li>
              </template>
            </ul>
          </div>
        </section>

        <JobList :jobs="jobs" :can-cancel="can('cancel_job')" @cancel="cancelJob" />
      </div>
      <div>
        <EventTimeline :events="events" :live="stream.connected.value && !finished" />
        <section v-if="jobs.length" class="card" data-testid="job-explain">
          <h2>Comprendre ce job</h2>
          <p class="small muted">L'assistant explique à partir des événements et des runbooks approuvés. Il ne relance ni n'approuve rien.</p>
          <div class="actions" style="margin-bottom: 0.75rem">
            <button type="button" data-testid="preview-context" @click="loadContextPreview">Aperçu du contexte</button>
            <button type="button" class="primary" data-testid="explain-job" @click="explainJob">Expliquer avec l'assistant</button>
          </div>
          <div v-if="contextPreview" class="small" data-testid="context-preview" style="margin-bottom: 0.75rem">
            <strong>Aperçu du contexte (filtré)</strong>
            <p class="muted">{{ contextPreview.note }}</p>
            <ul>
              <li v-for="ex in contextPreview.extracts" :key="ex.id">
                <label>
                  <input type="checkbox" :checked="!excludedExtracts.includes(ex.id)" @change="toggleExtract(ex.id)" />
                  [{{ ex.source }}] {{ ex.text }}
                </label>
              </li>
            </ul>
          </div>
          <p v-if="explain" class="small" style="white-space: pre-wrap" data-testid="explain-reply">{{ explain }}</p>
          <div v-if="diagnosis" class="small" data-testid="diagnosis" style="margin-top: 0.75rem">
            <p v-if="diagnosis.runbooks?.length">
              Runbooks :
              <span v-for="rb in diagnosis.runbooks" :key="`${rb.slug}@${rb.version}`" class="badge">{{ rb.slug }}@{{ rb.version }}</span>
            </p>
            <p><strong>Faits observés</strong></p>
            <ul>
              <li v-for="f in diagnosis.facts" :key="f.event_id">{{ f.at }} — {{ f.text }}</li>
            </ul>
            <p><strong>Hypothèses</strong> (non exécutables)</p>
            <ul>
              <li v-for="(h, i) in diagnosis.hypotheses" :key="i">{{ h.text }}</li>
            </ul>
            <p v-if="diagnosis.remediation" data-testid="remediation-draft">
              {{ diagnosis.remediation.note }}
            </p>
          </div>
        </section>
      </div>
    </div>
  </div>
  <div v-else>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>
    <p v-else class="empty">Chargement…</p>
  </div>
</template>
