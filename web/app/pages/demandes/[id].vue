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

async function explainJob() {
  explain.value = 'Analyse en cours…'
  try {
    const conv = await api.post(`/projects/${change.value.project.slug}/conversations`, {})
    const lastJob = jobs.value[jobs.value.length - 1]
    const res = await api.post(`/conversations/${conv.id}/messages`, { message: 'Explique-moi ce job', mode: 'explain', job_id: lastJob?.id })
    explain.value = res.turn?.reply || res.notice || "L'assistant n'a pas pu répondre."
  } catch (e) {
    explain.value = errorText(e)
  }
}

const resultTone = computed(() => SERVICE_TONE[service.value?.service_status] || 'neutral')
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

    <!-- Résultat : l'état du service vient des contrôles de santé, pas du succès des jobs. -->
    <div v-if="service" class="result" :class="resultTone" data-testid="service-result">
      <div>
        <div class="big">
          <template v-if="change.state === 'succeeded' && service.service_status === 'ready'">Succès — service prêt</template>
          <template v-else>Service : {{ service.service_status_label }}</template>
        </div>
        <div class="small">{{ service.service_reason || 'Pas encore vérifié.' }}
          <span v-if="service.service_checked_at"> · vérifié le {{ dateTime(service.service_checked_at) }}</span></div>
      </div>
    </div>
    <div v-if="change.reason && !service" class="banner info">{{ change.reason }}</div>

    <div class="grid-main">
      <div>
        <PlanPanel v-if="change.plan" :plan="change.plan">
          <div v-if="can('approve') || can('reject')" class="card" style="margin-top: 1rem; background: var(--surface-2)" data-testid="approval">
            <h3>Votre décision</h3>
            <p class="small">Vous approuvez exactement ce plan et cette empreinte. Une nouvelle version du besoin révoquerait l'approbation.</p>
            <label for="approval-comment">Commentaire <span class="muted small">(obligatoire pour un refus)</span></label>
            <textarea id="approval-comment" v-model="comment" maxlength="1000" />
            <div class="actions" style="margin-top: 0.5rem">
              <button v-if="can('approve')" type="button" class="primary" :disabled="busy" data-testid="approve" @click="decide('approved')">{{ label('approve') }}</button>
              <button v-if="can('reject')" type="button" class="danger" :disabled="busy || !comment.trim()" @click="decide('rejected')">{{ label('reject') }}</button>
            </div>
          </div>
          <p v-else-if="change.state === 'plan_ready'" class="banner info small" data-testid="awaiting-approval">
            En attente d'approbation par {{ change.revision?.summary?.approval }}.
          </p>
        </PlanPanel>

        <section v-if="change.pending_retry" class="card" data-testid="pending-retry">
          <h2>Reprise de la configuration demandée</h2>
          <p class="small">Hôtes concernés : {{ change.pending_retry.host_ids.length }} · demandée le {{ dateTime(change.pending_retry.requested_at) }}</p>
          <template v-if="can('approve_retry') || can('reject_retry')">
            <label for="retry-comment">Commentaire</label>
            <textarea id="retry-comment" v-model="comment" maxlength="1000" />
            <div class="actions" style="margin-top: 0.5rem">
              <button v-if="can('approve_retry')" type="button" class="primary" :disabled="busy" @click="decideRetry('approved')">{{ label('approve_retry') }}</button>
              <button v-if="can('reject_retry')" type="button" class="danger" :disabled="busy" @click="decideRetry('rejected')">{{ label('reject_retry') }}</button>
            </div>
          </template>
          <p v-else class="small muted">En attente d'un approbateur.</p>
        </section>

        <div v-if="can('request_retry')" class="banner warn">
          VM créée, configuration incomplète.
          <button type="button" class="primary" :disabled="busy" style="margin-left: 0.5rem" @click="requestRetry">{{ label('request_retry') }}</button>
        </div>

        <PlanDetails v-if="!change.plan && change.revision?.details" :details="change.revision.details" />

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
                <td>{{ h.deployment_state }}</td>
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
        <section v-if="jobs.length" class="card">
          <h2>Comprendre ce job</h2>
          <p class="small muted">L'assistant explique à partir des événements, cités comme données non fiables. Il ne relance rien.</p>
          <button type="button" @click="explainJob">Expliquer avec l'assistant</button>
          <p v-if="explain" class="small" style="white-space: pre-wrap">{{ explain }}</p>
        </section>
      </div>
    </div>
  </div>
  <div v-else>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>
    <p v-else class="empty">Chargement…</p>
  </div>
</template>
