<script setup lang="ts">
/** F53 : demandes déjà ouvertes potentiellement similaires (pas de fusion). */
const props = defineProps<{
  payload: any | null
}>()

const matches = computed(() => props.payload?.matches || [])
</script>

<template>
  <section
    v-if="matches.length"
    class="banner warn"
    role="status"
    data-testid="similar-changes"
  >
    <p data-testid="similar-notice">
      {{ payload?.notice || 'Des demandes similaires sont déjà ouvertes.' }}
    </p>
    <ul style="margin: 0.5rem 0 0; padding-left: 1.2rem">
      <li v-for="m in matches" :key="m.change_id" data-testid="similar-item">
        <NuxtLink :to="`/demandes/${m.change_id}`" data-testid="similar-open">
          {{ m.ref }} · {{ m.title }}
        </NuxtLink>
        — {{ m.state_label }}
        <span class="small muted">({{ (m.similarity?.reasons || []).join(', ') }})</span>
      </li>
    </ul>
    <p class="small muted" style="margin-top: 0.5rem">
      Vous pouvez ouvrir une demande existante, la suivre si vous y êtes autorisé, ou poursuivre séparément.
      Aucune fusion automatique.
    </p>
  </section>
</template>
