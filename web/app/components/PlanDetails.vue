<script setup lang="ts">
// Panneau « Détails du plan » : lecture seule, aucun contrôle éditable (invariant 2).
const props = defineProps<{ details: any; open?: boolean }>()

function show(v: any): string {
  if (v === null || v === undefined) return '—'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
</script>

<template>
  <details class="panel" :open="props.open" data-testid="plan-details">
    <summary>Détails du plan <span class="muted small">(lecture seule)</span></summary>
    <div class="panel-body">
      <p class="small muted">
        Valeurs déterminées par Rackloom à partir du profil de service, des politiques de l'environnement et de l'inventaire.
        Elles ne se modifient pas dans la demande.
      </p>
      <p class="small">
        Inventaire observé le {{ dateTime(props.details.inventory?.observed_at) }}
        <SimBadge v-if="props.details.inventory?.simulated" title="Inventaire vCenter simulé (connecteur fictif)" />
      </p>
      <section v-for="g in props.details.groups" :key="g.group">
        <h3>{{ g.label }}</h3>
        <table>
          <thead>
            <tr><th scope="col">Réglage</th><th scope="col">Valeur</th><th scope="col">Provenance</th></tr>
          </thead>
          <tbody>
            <tr v-for="v in g.values" :key="v.pointer">
              <td><code>{{ v.pointer }}</code></td>
              <td>
                {{ v.label || show(v.value) }}
                <span v-if="v.label && v.label !== show(v.value)" class="muted small mono"> ({{ show(v.value) }})</span>
                <span v-if="v.applied === false" class="badge neutral" title="Résolu et affiché, non appliqué au lot 1">Non appliqué</span>
              </td>
              <td>
                <ProvenanceBadge :source="v.source" :label="v.source_label" />
                <span v-if="v.candidates" class="muted small"> · {{ v.candidates }} candidat(s)</span>
                <div v-if="v.ref" class="muted small mono">{{ v.ref }}</div>
              </td>
            </tr>
          </tbody>
        </table>
      </section>
      <h3>Étapes simulées</h3>
      <ul>
        <li v-for="s in props.details.simulated_steps" :key="s.key">{{ s.label }} <SimBadge /></li>
      </ul>
      <template v-if="props.details.preview_limits?.length">
        <h3>Limites de l'aperçu</h3>
        <ul><li v-for="l in props.details.preview_limits" :key="l">{{ l }}</li></ul>
      </template>
      <slot />
    </div>
  </details>
</template>
