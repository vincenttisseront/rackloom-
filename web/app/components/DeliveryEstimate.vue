<script setup lang="ts">
/** F56 : estimation de mise à disposition (sans modèle). */
const props = defineProps<{ estimate: any | null }>()

function fmtSeconds(s: number | null | undefined): string {
  if (s == null) return '—'
  if (s < 60) return `${s} s`
  const m = Math.round(s / 60)
  if (m < 60) return `≈ ${m} min`
  return `≈ ${(m / 60).toFixed(1)} h`
}

function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return dateTime(iso)
  } catch {
    return iso
  }
}
</script>

<template>
  <section
    v-if="estimate"
    class="card"
    aria-labelledby="estimate-title"
    data-testid="delivery-estimate"
  >
    <h2 id="estimate-title">Mise à disposition</h2>
    <p class="small muted" data-testid="estimate-notice">{{ estimate.notice }}</p>
    <dl class="kv">
      <dt>Approbation</dt>
      <dd data-testid="estimate-approval">
        {{ estimate.approval?.note || estimate.approval?.status }}
        <span v-if="!estimate.approval?.guaranteed_delivery_at" class="badge">Pas de date garantie</span>
      </dd>
      <dt>Créneau</dt>
      <dd data-testid="estimate-schedule">
        <template v-if="estimate.schedule?.run_after || estimate.schedule?.next_window">
          Prochain créneau : {{ fmtWhen(estimate.schedule.run_after || estimate.schedule.next_window) }}
          <span v-if="estimate.schedule.timezone" class="small muted">({{ estimate.schedule.timezone }})</span>
        </template>
        <template v-else>{{ estimate.schedule?.note || 'Aucun créneau particulier' }}</template>
      </dd>
      <dt>Exécution</dt>
      <dd data-testid="estimate-execution">
        <template v-if="estimate.execution?.status === 'estimated' && estimate.execution.range">
          Fourchette : {{ fmtSeconds(estimate.execution.range.p50_seconds) }}
          (p50) — {{ fmtSeconds(estimate.execution.range.p90_seconds) }} (p90)
        </template>
        <template v-else>
          <span class="badge warn">{{ estimate.execution?.label || 'délai non estimable' }}</span>
          <span class="small muted"> — {{ estimate.execution?.note }}</span>
        </template>
      </dd>
      <dt>File d'attente</dt>
      <dd data-testid="estimate-queue">
        <span class="badge">{{ estimate.queue?.status === 'unavailable' ? 'Non disponible' : estimate.queue?.status }}</span>
        <span class="small muted"> — {{ estimate.queue?.reason }}</span>
      </dd>
    </dl>
    <p v-if="estimate.known_blockers?.length" class="banner warn small" data-testid="estimate-blockers">
      Blocage connu : {{ estimate.known_blockers.join(' · ') }}
    </p>
    <p v-if="estimate.model_used === false" class="small muted" data-testid="estimate-no-model">
      Aucune estimation demandée à un modèle.
    </p>
  </section>
</template>
