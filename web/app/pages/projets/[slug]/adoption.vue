<script setup lang="ts">
const route = useRoute()
const api = useApi()
const slug = computed(() => String(route.params.slug))
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const busy = ref(false)
const candidates = ref<any[]>([])
const adoptions = ref<any[]>([])
const selected = ref<any | null>(null)

async function refresh() {
  ;[candidates.value, adoptions.value] = await Promise.all([
    api.get(`/projects/${slug.value}/adoptions/candidates?environment=dev`).then((r: any) => r.candidates || []),
    api.get(`/projects/${slug.value}/adoptions`),
  ])
}

onMounted(async () => {
  try {
    await refresh()
  } catch (e) {
    error.value = errorText(e)
  }
})

async function enroll(vm: any) {
  busy.value = true
  error.value = null
  notice.value = null
  try {
    selected.value = await api.post(`/projects/${slug.value}/adoptions`, {
      environment: 'dev',
      vm_stable_id: vm.vm_stable_id,
    })
    notice.value = 'VM enrôlée pour observation en lecture seule (fictive).'
    await refresh()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function openAdoption(id: string) {
  busy.value = true
  error.value = null
  try {
    selected.value = await api.get(`/adoptions/${id}`)
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function run(action: 'observe' | 'preview' | 'propose-import' | 'approve' | 'import') {
  if (!selected.value) return
  busy.value = true
  error.value = null
  notice.value = null
  try {
    selected.value = await api.post(`/adoptions/${selected.value.id}/${action}`)
    if (action === 'preview') {
      notice.value = selected.value.preview_result?.notice
        || 'Aperçu seul : la VM n’a pas été modifiée.'
    } else if (action === 'propose-import') {
      notice.value = 'Proposition d’import enregistrée (non exécutée).'
    } else if (action === 'import') {
      notice.value = 'Import simulé enregistré.'
    }
    await refresh()
  } catch (e) {
    error.value = errorText(e)
    try {
      selected.value = await api.get(`/adoptions/${selected.value.id}`)
      await refresh()
    } catch {
      /* ignore */
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <div class="crumbs">
          <NuxtLink to="/">Accueil</NuxtLink> ›
          <NuxtLink :to="`/projets/${slug}`">{{ slug }}</NuxtLink> › Adoption
        </div>
        <h1>Adoption de VM existantes</h1>
        <p class="small muted">
          Enrôlement et observation en lecture seule d’abord. L’aperçu d’import ne modifie pas la VM.
          Un conflit de propriété bloque l’import.
          <SimBadge />
        </p>
      </div>
    </div>

    <div v-if="error" class="banner bad" role="alert" data-testid="adoption-error">{{ error }}</div>
    <div v-if="notice" class="banner ok" role="status" data-testid="adoption-notice">{{ notice }}</div>

    <section class="card" aria-labelledby="cand-title" data-testid="adoption-candidates">
      <h2 id="cand-title">Candidats (inventaire fictif)</h2>
      <p class="small muted">Observations fictives explicites — pas de laboratoire vSphere réel.</p>
      <p v-if="!candidates.length" class="empty">Aucun candidat.</p>
      <table v-else>
        <thead>
          <tr>
            <th scope="col">Nom</th>
            <th scope="col">Identité</th>
            <th scope="col">Propriété</th>
            <th scope="col">Action</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in candidates" :key="c.vm_stable_id" :data-testid="`candidate-${c.vm_stable_id}`">
            <td>{{ c.name }}</td>
            <td class="mono small">{{ c.vm_stable_id }}</td>
            <td>
              <span class="badge" :class="c.conflict ? 'bad' : 'ok'">{{ c.ownership }}</span>
              <span v-if="c.conflict" class="small muted"> conflit</span>
            </td>
            <td>
              <button
                type="button"
                class="primary"
                :disabled="busy || c.already_enrolled || c.already_host"
                data-testid="enroll-vm"
                @click="enroll(c)"
              >
                Enrôler (lecture seule)
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="card" aria-labelledby="ado-title" data-testid="adoption-list">
      <h2 id="ado-title">Adoptions</h2>
      <p v-if="!adoptions.length" class="empty">Aucune adoption.</p>
      <ul v-else>
        <li v-for="a in adoptions" :key="a.id">
          <button type="button" class="link" data-testid="open-adoption" @click="openAdoption(a.id)">
            {{ a.vm_name }}
          </button>
          <span class="badge" :class="a.state === 'blocked_conflict' ? 'bad' : a.state === 'imported' ? 'ok' : 'warn'">
            {{ a.state_label }}
          </span>
        </li>
      </ul>
    </section>

    <section v-if="selected" class="card" aria-labelledby="detail-title" data-testid="adoption-detail">
      <h2 id="detail-title">{{ selected.vm_name }}</h2>
      <dl class="kv">
        <dt>État</dt><dd data-testid="adoption-state">{{ selected.state_label }}</dd>
        <dt>Propriété</dt><dd>{{ selected.ownership }}</dd>
        <dt>Conflit</dt><dd>{{ selected.conflict_reason || '—' }}</dd>
        <dt>Observation</dt>
        <dd>
          <span v-if="selected.observation_fictional">Fictive <SimBadge /></span>
          <span v-else>réelle</span>
          · mutation={{ selected.observation?.mutation === false ? 'non' : '?' }}
        </dd>
        <dt>Proposition</dt>
        <dd class="mono small">{{ selected.import_proposal_sha256 || 'aucune' }}</dd>
      </dl>
      <div class="actions" style="flex-wrap: wrap">
        <button type="button" :disabled="busy" data-testid="adoption-observe" @click="run('observe')">
          Observer
        </button>
        <button type="button" :disabled="busy" data-testid="adoption-preview" @click="run('preview')">
          Aperçu d’import
        </button>
        <button type="button" :disabled="busy" data-testid="adoption-propose" @click="run('propose-import')">
          Proposer l’import
        </button>
        <button type="button" :disabled="busy" data-testid="adoption-approve" @click="run('approve')">
          Approuver
        </button>
        <button type="button" class="primary" :disabled="busy" data-testid="adoption-import" @click="run('import')">
          Exécuter l’import
        </button>
      </div>
      <p v-if="selected.preview" class="small muted" data-testid="adoption-preview-notice">
        {{ selected.preview.notice || selected.preview_result?.notice }}
      </p>
    </section>
  </div>
</template>
