<script setup lang="ts">
// Assistant : propose un brouillon, ne décide rien. Chaque proposition est revalidée par le serveur
// et n'entre dans le formulaire qu'après une action explicite de l'utilisateur.
const props = defineProps<{ project: string; need: () => any }>()
const emit = defineEmits<{ apply: [patch: { path: string; value: any }]; used: [] }>()

const api = useApi()
const status = ref<{ available: boolean; simulated: boolean; adapter: string; routing?: any } | null>(null)
const conversationId = ref<string | null>(null)
const message = ref('')
const sending = ref(false)
const turns = ref<{ who: 'me' | 'bot'; text: string; patch?: any[]; questions?: any[]; notice?: string | null; tone?: string }[]>([])
const applied = ref<Set<string>>(new Set())
const down = ref<string | null>(null)

const PATH_LABELS: Record<string, string> = {
  '/service/template': 'Service',
  '/environment': 'Environnement',
  '/inputs/quantity': 'Nombre de serveurs',
  '/inputs/size_tier': 'Niveau',
  '/inputs/usage': 'Usage',
  '/inputs/lifetime_until': 'Date de fin',
}

onMounted(async () => {
  try {
    status.value = await api.get('/assistant/status')
  } catch {
    status.value = { available: false, simulated: false, adapter: 'disabled' }
  }
})

async function send() {
  const text = message.value.trim()
  if (!text || sending.value) return
  sending.value = true
  turns.value.push({ who: 'me', text })
  message.value = ''
  try {
    if (!conversationId.value) {
      conversationId.value = (await api.post(`/projects/${props.project}/conversations`, {})).id
    }
    const res = await api.post(`/conversations/${conversationId.value}/messages`, { message: text, need: props.need() })
    emit('used')
    down.value = res.status === 'unavailable' ? res.notice : null
    if (res.status === 'ok' && res.turn) {
      turns.value.push({ who: 'bot', text: res.turn.reply, patch: res.turn.need_patch, questions: res.turn.questions, notice: res.notice })
    } else {
      turns.value.push({ who: 'bot', text: res.notice || "L'assistant n'a pas pu répondre. Le formulaire reste disponible.", tone: 'warn' })
    }
  } catch (e) {
    turns.value.push({ who: 'bot', text: errorText(e), tone: 'warn' })
  } finally {
    sending.value = false
  }
}

function apply(p: { path: string; value: any }, key: string) {
  emit('apply', p)
  applied.value = new Set([...applied.value, key])
}
</script>

<template>
  <section class="card" aria-labelledby="assistant-title" data-testid="assistant">
    <div class="field-head">
      <h2 id="assistant-title">Assistant</h2>
      <SimBadge v-if="status?.simulated" label="Assistant fictif" title="Adaptateur fictif déterministe : aucun modèle n'est interrogé" />
    </div>
    <div v-if="status && !status.available" class="banner warn" role="status" data-testid="assistant-unavailable">
      L'assistant est {{ status.adapter === 'disabled' ? 'désactivé' : 'indisponible' }}. Le formulaire permet de décrire
      tout le besoin ; les contrôles et l'approbation sont les mêmes.
    </div>
    <div
      v-else-if="status?.routing?.tasks?.draft && status.routing.tasks.draft.available === false"
      class="banner warn"
      role="status"
      data-testid="assistant-routing-down"
    >
      Le modèle de rédaction est indisponible. Le formulaire reste utilisable ; aucun repli cloud n'est tenté.
    </div>
    <template v-else>
      <div v-if="down" class="banner warn" role="status" data-testid="assistant-unavailable">
        {{ down.includes('analyse') ? down : "L'assistant est indisponible pour le moment. Le formulaire permet de décrire tout le besoin ; les contrôles et l'approbation sont les mêmes." }}
      </div>
      <p class="small muted">
        Décrivez votre besoin en langage courant. L'assistant propose ; vous confirmez chaque valeur dans le formulaire.
        Il ne peut ni soumettre, ni approuver.
      </p>
      <div class="chat" aria-live="polite">
        <div v-for="(t, i) in turns" :key="i" class="bubble" :class="t.who" :data-tone="t.tone">
          <span class="sr-only">{{ t.who === 'me' ? 'Vous' : 'Assistant' }} : </span>{{ t.text }}
          <div v-if="t.notice" class="small muted">{{ t.notice }}</div>
          <ul v-if="t.questions?.length" class="small">
            <li v-for="q in t.questions" :key="q.field">{{ q.text }}</li>
          </ul>
          <div v-for="(p, j) in t.patch || []" :key="`${i}-${j}`" class="patch small">
            <ProvenanceBadge source="suggestion" />
            {{ PATH_LABELS[p.path] || p.path }} : <strong>{{ p.value }}</strong>
            <button type="button" :disabled="applied.has(`${i}-${j}`)" @click="apply(p, `${i}-${j}`)">
              {{ applied.has(`${i}-${j}`) ? 'Reporté dans le formulaire' : 'Reporter dans le formulaire' }}
            </button>
          </div>
        </div>
      </div>
      <form class="actions" style="margin-top: 0.75rem" @submit.prevent="send">
        <label for="assistant-input" class="sr-only">Message pour l'assistant</label>
        <input id="assistant-input" v-model="message" type="text" maxlength="2000" placeholder="Ex. : je veux une VM Ubuntu de test avec Nginx" style="flex: 1" />
        <button type="submit" class="primary" :disabled="sending || !message.trim()">Envoyer</button>
      </form>
    </template>
  </section>
</template>
