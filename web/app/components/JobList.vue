<script setup lang="ts">
const props = defineProps<{ jobs: any[]; canCancel?: boolean }>()
const emit = defineEmits<{ cancel: [jobId: string] }>()
const ACTIVE = ['queued', 'leased', 'running']
</script>

<template>
  <section class="card" aria-labelledby="jobs-title">
    <h2 id="jobs-title">Exécution</h2>
    <p v-if="!props.jobs.length" class="empty">Aucun job pour l'instant.</p>
    <div v-for="j in props.jobs" :key="j.id" class="job" data-testid="job">
      <div class="field-head">
        <h3>{{ JOB_LABELS[j.kind] || j.kind }}</h3>
        <span class="badge" :class="j.state === 'succeeded' ? 'ok' : j.state === 'failed' || j.state === 'blocked' ? 'bad' : j.state === 'uncertain' ? 'warn' : 'info'">
          {{ JOB_STATES[j.state] || j.state }}
        </span>
      </div>
      <p v-if="j.blocked_reason" class="field-error" data-testid="job-blocked-reason">{{ BLOCK_REASONS[j.blocked_reason] || j.blocked_reason }}</p>
      <p v-if="j.state === 'queued' && j.wait_reason" class="small muted" data-testid="job-wait-reason">
        En attente : {{ j.wait_reason_label || BLOCK_REASONS[j.wait_reason] || j.wait_reason }}
      </p>
      <p v-if="j.cancel_requested_at" class="small">Annulation demandée le {{ dateTime(j.cancel_requested_at) }} (non garantie)</p>
      <ul class="small">
        <li v-for="s in j.steps" :key="s.seq">
          <span :class="{ 'field-error': s.state === 'failed' }">{{ s.name }}</span>
          <SimBadge v-if="s.simulated" />
          <span v-if="s.state === 'failed' && s.summary?.reason" class="muted"> — {{ s.summary.reason }}</span>
        </li>
      </ul>
      <button v-if="props.canCancel && ACTIVE.includes(j.state) && !j.cancel_requested_at" type="button" class="danger" @click="emit('cancel', j.id)">
        Demander l'annulation
      </button>
    </div>
  </section>
</template>
