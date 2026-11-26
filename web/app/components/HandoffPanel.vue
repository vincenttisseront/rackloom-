<script setup lang="ts">
/** F54 : passer la main sans perdre le contexte. */
const props = defineProps<{ changeId: string }>()
const api = useApi()

const items = ref<any[]>([])
const diffs = ref<any>(null)
const candidates = ref<any[]>([])
const notice = ref<string | null>(null)
const error = ref<string | null>(null)
const busy = ref(false)
const solicited = ref('')
const question = ref('')
const replyDraft = ref<Record<string, string>>({})

async function refresh() {
  error.value = null
  try {
    const [rev, cands] = await Promise.all([
      api.get(`/changes/${props.changeId}/reviews`),
      api.get(`/changes/${props.changeId}/review-candidates`),
    ])
    items.value = rev.items || []
    diffs.value = rev.diffs_since_last_review
    notice.value = rev.notice
    candidates.value = cands
  } catch (e) {
    error.value = errorText(e)
  }
}

onMounted(refresh)

async function solicit() {
  if (!solicited.value || !question.value.trim() || busy.value) return
  busy.value = true
  error.value = null
  try {
    await api.post(`/changes/${props.changeId}/reviews`, {
      solicited_user_id: solicited.value,
      question: question.value.trim(),
    })
    question.value = ''
    await refresh()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function reply(reviewId: string) {
  const body = (replyDraft.value[reviewId] || '').trim()
  if (!body || busy.value) return
  busy.value = true
  error.value = null
  try {
    await api.post(`/changes/${props.changeId}/reviews/${reviewId}/replies`, { body })
    replyDraft.value[reviewId] = ''
    await refresh()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="card" aria-labelledby="handoff-title" data-testid="handoff-panel">
    <h2 id="handoff-title">Passer la main</h2>
    <p class="small muted">{{ notice || 'Sollicitez un collègue déjà habilité. Aucun droit n\'est créé par le partage.' }}</p>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>

    <div v-if="diffs?.notes?.length" class="banner info" data-testid="handoff-diffs">
      <p v-for="(n, i) in diffs.notes" :key="i">{{ n }}</p>
    </div>

    <form class="field" data-testid="handoff-form" @submit.prevent="solicit">
      <label for="handoff-who">Collègue habilité</label>
      <select id="handoff-who" v-model="solicited" required data-testid="handoff-candidate">
        <option value="" disabled>Choisir…</option>
        <option v-for="c in candidates" :key="c.id" :value="c.id">{{ c.display_name }} ({{ c.email }})</option>
      </select>
      <label for="handoff-q">Question</label>
      <textarea id="handoff-q" v-model="question" rows="3" maxlength="2000" required data-testid="handoff-question" />
      <button type="submit" class="button primary" :disabled="busy" data-testid="handoff-submit">Solliciter</button>
    </form>

    <ul v-if="items.length" class="stack" style="list-style: none; padding: 0; margin-top: 1rem">
      <li v-for="r in items" :key="r.id" class="field" data-testid="handoff-item" :data-status="r.status">
        <p class="small muted">Version {{ r.version_no }} · {{ r.status }}</p>
        <p><strong>{{ r.created_by?.display_name }}</strong> → {{ r.solicited?.display_name }}</p>
        <p data-testid="handoff-question-text">{{ r.question }}</p>
        <p v-if="r.reply_text" data-testid="handoff-reply-text">Réponse : {{ r.reply_text }}</p>
        <p class="small muted">{{ r.notice }}</p>
        <div v-if="r.status === 'open'" class="field">
          <label :for="`reply-${r.id}`">Répondre</label>
          <textarea
            :id="`reply-${r.id}`"
            v-model="replyDraft[r.id]"
            rows="2"
            maxlength="4000"
            data-testid="handoff-reply"
          />
          <button type="button" class="button" :disabled="busy" data-testid="handoff-reply-submit" @click="reply(r.id)">
            Envoyer la réponse
          </button>
        </div>
      </li>
    </ul>
  </section>
</template>
