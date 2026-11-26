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
      <section v-if="props.details.capacity" data-testid="capacity-panel" aria-labelledby="cap-title">
        <h3 id="cap-title">Capacité du placement</h3>
        <p class="small muted">{{ props.details.capacity.notice }}</p>
        <p class="small">
          Mesures observées le {{ dateTime(props.details.capacity.observed_at) }}
          <span v-if="props.details.capacity.age_seconds != null" class="muted">
            (âge {{ Math.round(props.details.capacity.age_seconds / 60) }} min)
          </span>
          <SimBadge v-if="props.details.capacity.simulated" title="Mesures de capacité simulées" />
        </p>
        <table>
          <thead>
            <tr>
              <th scope="col">Ressource</th>
              <th scope="col">Cible</th>
              <th scope="col">Libre</th>
              <th scope="col">Besoin</th>
              <th scope="col">Unité</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in props.details.capacity.resources" :key="r.resource">
              <td>{{ r.resource }}</td>
              <td>{{ r.target || '—' }}</td>
              <td>{{ r.free ?? '—' }}</td>
              <td>{{ r.need ?? '—' }}</td>
              <td>{{ r.unit }}</td>
            </tr>
          </tbody>
        </table>
      </section>
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
