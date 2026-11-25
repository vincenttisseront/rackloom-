<script setup lang="ts">
import type { RackloomEvent } from '~/composables/useEventStream'

const props = defineProps<{ events: RackloomEvent[]; live?: boolean }>()

function text(e: RackloomEvent): string {
  const d = e.data || {}
  switch (e.type) {
    case 'change.created': return `Demande ${d.ref} créée`
    case 'change.state': return `${d.label || d.to}${d.message ? ` : ${d.message}` : ''}`
    case 'job.queued': return `${d.label || JOB_LABELS[d.kind] || d.kind} : en file`
    case 'job.step': return `${d.step}${d.state === 'failed' ? ' — échec' : ''}`
    case 'job.failed': return `${JOB_LABELS[d.kind] || d.kind || 'Job'} : échec${d.reason ? ` (${d.reason})` : ''}`
    case 'job.blocked': return `${JOB_LABELS[d.kind] || d.kind || 'Job'} : bloqué à l'admission${d.reason ? ` (${d.reason})` : ''}`
    case 'job.uncertain': return `${JOB_LABELS[d.kind] || d.kind || 'Job'} : résultat incertain, réconciliation requise`
    case 'job.requeued': return `${JOB_LABELS[d.kind] || d.kind || 'Job'} : remis en file`
    case 'job.cancel_requested': return 'Annulation demandée (non garantie)'
    case 'job.cancel_signalled': return "Signal d'annulation transmis au runner"
    case 'execution.applied': return 'Plan approuvé appliqué'
    case 'deployment.service_status': return `État du service : ${d.label || d.status}${d.reason ? ` — ${d.reason}` : ''}`
    case 'retry.requested': return 'Reprise de la configuration demandée'
    case 'change.superseded': return 'Nouvelle version du besoin : approbation précédente révoquée'
    default: return d.label || d.message || e.type
  }
}
</script>

<template>
  <section class="card" aria-labelledby="timeline-title">
    <div class="field-head">
      <h2 id="timeline-title">Chronologie</h2>
      <span v-if="props.live" class="badge ok" title="Mise à jour en direct">En direct</span>
    </div>
    <p v-if="!props.events.length" class="empty">Aucun événement pour l'instant.</p>
    <ol class="timeline" aria-live="polite" data-testid="timeline">
      <li v-for="e in props.events" :key="e.id">
        <time :datetime="e.occurred_at">{{ time(e.occurred_at) }}</time>
        <div>
          <span :class="{ 'field-error': e.severity === 'error' }">{{ text(e) }}</span>
          <SimBadge v-if="e.data?.simulated" />
          <span class="muted small"> · {{ e.source }}</span>
        </div>
      </li>
    </ol>
    <p class="small muted">Heures de Paris.</p>
  </section>
</template>
