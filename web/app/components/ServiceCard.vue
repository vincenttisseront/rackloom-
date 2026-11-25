<script setup lang="ts">
const props = defineProps<{ card: any }>()
const showTech = ref(false)
const tone = computed(() => SERVICE_TONE[props.card?.status] || 'neutral')
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
    <details style="margin-top: 0.75rem" data-testid="service-technical" @toggle="showTech = ($event.target as HTMLDetailsElement).open">
      <summary class="small">Détails techniques</summary>
      <p class="small muted">{{ card.technical.note }}</p>
      <p class="small">Motif serveur : {{ card.technical.service_reason || '—' }}</p>
      <ul class="small">
        <li v-for="h in card.technical.hosts" :key="h.id">
          <NuxtLink :to="`/machines/${h.id}`">{{ h.name }}</NuxtLink> · {{ h.deployment_state }}
        </li>
      </ul>
    </details>
  </section>
</template>
