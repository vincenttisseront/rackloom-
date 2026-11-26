<script setup lang="ts">
/** F51 : exemples « qu'est-ce que je peux demander » — indépendants de l'assistant. */
const props = defineProps<{
  examples: any[] | null
  notice?: string | null
  projectSlug?: string | null
}>()

const filtered = computed(() => {
  const list = props.examples || []
  if (!props.projectSlug) return list
  return list.filter(e => e.project?.slug === props.projectSlug)
})

function href(ex: any): string {
  const slug = ex.project?.slug
  const q = new URLSearchParams({ example: ex.id })
  // Prefill via need encodé : le formulaire lit `example` puis charge via API projet.
  return `/projets/${slug}/nouvelle?${q.toString()}`
}
</script>

<template>
  <section
    v-if="filtered.length"
    class="card"
    aria-labelledby="examples-title"
    data-testid="request-examples"
  >
    <h2 id="examples-title">Qu'est-ce que je peux demander ?</h2>
    <p class="small muted" data-testid="examples-notice">
      {{ notice || "Exemples adaptés à vos droits et services octroyés." }}
    </p>
    <ul class="stack" style="list-style: none; padding: 0; margin: 0.75rem 0 0">
      <li
        v-for="ex in filtered"
        :key="ex.id"
        class="field"
        style="margin-bottom: 0.75rem"
        data-testid="example-item"
        :data-example-id="ex.id"
        :data-production="ex.is_production ? '1' : '0'"
      >
        <div class="field-head">
          <strong>{{ ex.title }}</strong>
          <span v-if="ex.is_production" class="badge warn">Production</span>
        </div>
        <p v-if="ex.description" class="small muted">{{ ex.description }}</p>
        <p class="small">
          <span class="badge ok" data-testid="example-can-submit">Vous pouvez soumettre cette demande</span>
          <span class="badge" data-testid="example-can-execute" title="execute_note">
            Pas encore autorisée à s'exécuter
          </span>
        </p>
        <p class="small muted">{{ ex.execute_note }}</p>
        <NuxtLink class="button primary" :to="href(ex)" data-testid="example-open">
          Ouvrir le parcours
        </NuxtLink>
      </li>
    </ul>
  </section>
</template>
