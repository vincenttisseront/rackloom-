<script setup lang="ts">
const props = defineProps<{ card: any }>()
const emit = defineEmits<{
  action: [id: string]
  requestAccess: [level: string]
  approveAccess: [id: string]
  rejectAccess: [id: string]
  retryAccess: [id: string]
  renewLease: []
  proposeRetirement: []
}>()
const showTech = ref(false)
const accessLevel = ref('')
const tone = computed(() => SERVICE_TONE[props.card?.status] || 'neutral')
const eligibleLevels = computed(() =>
  (props.card?.access_request?.levels || []).filter((l: any) => l.eligible)
)
const leaseTone = computed(() => {
  const state = props.card?.lease?.state
  if (state === 'expired' || state === 'retirement_proposed') return 'bad'
  if (state === 'reminded') return 'warn'
  return 'neutral'
})
watch(
  eligibleLevels,
  (levels) => {
    if (!accessLevel.value && levels.length) accessLevel.value = levels[0].id
  },
  { immediate: true },
)
</script>

<template>
  <section v-if="card" class="result" :class="tone" data-testid="service-card" :data-status="card.status">
    <div>
      <div class="big" data-testid="service-headline">{{ card.headline }}</div>
      <p v-if="card.detail" class="small" data-testid="service-detail">{{ card.detail }}</p>
      <p class="small muted">{{ card.notice }}</p>
    </div>
    <dl class="kv small" style="margin-top: 0.75rem" data-testid="service-summary">
      <dt>Service</dt>
      <dd>{{ card.service.title }} <span class="muted">{{ card.service.version }}</span></dd>
      <dt>Environnement</dt>
      <dd>{{ card.project.name }} / {{ card.environment.name }}
        <span v-if="card.environment.is_production" class="badge warn">Production</span>
      </dd>
      <dt>Accès</dt>
      <dd data-testid="service-access">{{ card.access_mode }}</dd>
      <dt>Responsable</dt>
      <dd>{{ card.owner || '—' }}</dd>
      <template v-if="card.outputs?.length">
        <dt>Noms publiés</dt>
        <dd>
          <span v-for="o in card.outputs" :key="`${o.key}-${o.value}`" class="mono" style="margin-right: 0.75rem">
            {{ o.label }} : {{ o.value }}
          </span>
        </dd>
      </template>
      <dt>Vérifications</dt>
      <dd data-testid="service-checks-summary">
        {{ card.checks.summary }}
        <span v-if="card.checked_at" class="muted"> · {{ dateTime(card.checked_at) }}</span>
      </dd>
      <template v-if="card.availability">
        <dt>État du service</dt>
        <dd data-testid="availability-service-state">{{ card.availability.service_state_label }}</dd>
        <dt>Santé de la sonde</dt>
        <dd data-testid="availability-probe-health">
          {{ card.availability.probe_health_label }}
          <span v-if="card.availability.from_segment" class="muted"> · segment {{ card.availability.from_segment }}</span>
        </dd>
        <dt>Fraîcheur</dt>
        <dd data-testid="availability-freshness">{{ card.availability.freshness_label }}</dd>
      </template>
    </dl>
    <ul v-if="card.checks.succeeded.length" class="small" data-testid="service-checks-ok">
      <li v-for="(c, i) in card.checks.succeeded" :key="`ok-${i}`">
        <span class="badge ok">OK</span> {{ c.host }} · {{ c.label }} : {{ c.evidence }}
      </li>
    </ul>
    <ul v-if="card.checks.failed.length" class="small" data-testid="service-checks-ko">
      <li v-for="(c, i) in card.checks.failed" :key="`ko-${i}`">
        <span class="badge bad">KO</span> {{ c.host }} · {{ c.label }} : {{ c.failure || c.evidence }}
      </li>
    </ul>

    <!-- F58 : actions courantes (demande préremplie, impact visible) -->
    <div v-if="card.service_actions?.length" class="actions" style="margin-top: 1rem" data-testid="service-actions">
      <button
        v-for="a in card.service_actions"
        :key="a.id"
        type="button"
        :data-testid="`service-action-${a.id}`"
        @click="emit('action', a.id)"
      >
        {{ a.label }}
      </button>
      <ul class="small" style="margin-top: 0.5rem">
        <li v-for="a in card.service_actions" :key="`impact-${a.id}`" data-testid="service-action-impact">
          <span class="badge warn">Impact</span> {{ a.label }} : {{ a.impact_label }}
        </li>
      </ul>
      <p class="small muted">Chaque action ouvre une demande préremplie (plan et approbation). Aucune commande directe.</p>
    </div>

    <!-- F06 : bail temporaire -->
    <div
      v-if="card.lease"
      class="banner"
      :class="leaseTone"
      style="margin-top: 1rem"
      data-testid="lease-panel"
      :data-state="card.lease.state"
    >
      <p class="small" style="margin: 0">
        <strong>Bail</strong>
        · {{ card.lease.state_label }}
        · jusqu'au {{ card.lease.expires_on }}
        <template v-if="card.lease.owner"> · {{ card.lease.owner }}</template>
        <template v-if="typeof card.lease.days_remaining === 'number' && !card.lease.expired">
          · {{ card.lease.days_remaining }} j. restants
        </template>
      </p>
      <p v-if="card.lease.expired" class="small" data-testid="lease-expired-notice" style="margin: 0.35rem 0 0">
        Labo expiré : signalé, ressources intactes tant que le retrait n'est pas approuvé et exécuté.
      </p>
      <div v-if="card.lease.actions?.length" class="actions" style="margin-top: 0.5rem">
        <button
          v-if="card.lease.can_renew"
          type="button"
          data-testid="lease-renew"
          @click="emit('renewLease')"
        >
          Renouveler
        </button>
        <button
          v-if="card.lease.can_propose_retirement"
          type="button"
          data-testid="lease-propose-retirement"
          @click="emit('proposeRetirement')"
        >
          Proposer le retrait
        </button>
      </div>
    </div>

    <!-- F59 : demander l'accès -->
    <div v-if="card.access_request" class="card" style="margin-top: 1rem" data-testid="access-request-panel">
      <h2 class="small" style="margin: 0 0 0.5rem">Demander l'accès</h2>
      <p class="small muted">{{ card.access_request.notice }}</p>
      <div v-if="card.access_request.can_request && eligibleLevels.length" class="actions" style="margin-top: 0.5rem">
        <label class="small" for="access-level">Niveau</label>
        <select id="access-level" v-model="accessLevel" data-testid="access-level">
          <option v-for="l in eligibleLevels" :key="l.id" :value="l.id">{{ l.label }}</option>
        </select>
        <button
          type="button"
          class="primary"
          data-testid="request-access"
          :disabled="!accessLevel"
          @click="emit('requestAccess', accessLevel)"
        >
          Demander
        </button>
      </div>
      <ul v-if="card.access_request.requests?.length" class="small" style="margin-top: 0.75rem" data-testid="access-requests">
        <li v-for="r in card.access_request.requests" :key="r.id" :data-state="r.state">
          <span class="badge" :class="r.state === 'granted' ? 'ok' : r.state === 'sync_failed' ? 'bad' : 'neutral'">
            {{ r.state_label }}
          </span>
          {{ r.access_level }} · {{ r.subject?.display_name }}
          <template v-if="r.sync_error"> — {{ r.sync_error }}</template>
          <span v-for="act in r.allowed_actions" :key="act.action" style="margin-left: 0.5rem">
            <button
              v-if="act.action === 'approve'"
              type="button"
              data-testid="approve-access"
              @click="emit('approveAccess', r.id)"
            >{{ act.label }}</button>
            <button
              v-else-if="act.action === 'reject'"
              type="button"
              data-testid="reject-access"
              @click="emit('rejectAccess', r.id)"
            >{{ act.label }}</button>
            <button
              v-else-if="act.action === 'retry_sync'"
              type="button"
              data-testid="retry-access"
              @click="emit('retryAccess', r.id)"
            >{{ act.label }}</button>
          </span>
        </li>
      </ul>
    </div>

    <details style="margin-top: 0.75rem" data-testid="service-technical" @toggle="showTech = ($event.target as HTMLDetailsElement).open">
      <summary class="small">Détails techniques</summary>
      <p class="small muted">{{ card.technical.note }}</p>
      <p class="small">Motif serveur : {{ card.technical.service_reason || '—' }}</p>
      <ul class="small">
        <li v-for="h in card.technical.hosts" :key="h.id">
          <NuxtLink :to="`/machines/${h.id}`">{{ h.name }}</NuxtLink>
          <span v-if="h.composition_member" class="mono"> [{{ h.composition_member }}]</span>
          · {{ h.deployment_state }}
        </li>
      </ul>
    </details>
  </section>
</template>
