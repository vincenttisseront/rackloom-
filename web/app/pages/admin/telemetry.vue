<script setup lang="ts">
const api = useApi()
const session = useSession()
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const agents = ref<any[]>([])
const busy = ref(false)
const form = ref({
  slug: '',
  name: '',
  project: 'vitrine',
})
const lastCert = ref<any | null>(null)

const isAdmin = computed(() => hasRole(session.value, 'platform_admin'))

async function load() {
  error.value = null
  try {
    agents.value = await api.get('/admin/telemetry/agents')
  } catch (e) {
    error.value = errorText(e)
  }
}

async function enroll() {
  busy.value = true
  error.value = null
  notice.value = null
  lastCert.value = null
  try {
    const body = await api.post('/admin/telemetry/agents/enroll', {
      slug: form.value.slug.trim(),
      name: form.value.name.trim(),
      project: form.value.project.trim(),
    })
    lastCert.value = body.client_certificate
    notice.value = `Agent « ${body.agent.slug} » enrôlé. La clé privée n'est affichée qu'une fois.`
    form.value.slug = ''
    form.value.name = ''
    await load()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

async function revoke(id: string) {
  busy.value = true
  error.value = null
  try {
    await api.post(`/admin/telemetry/agents/${id}/revoke`, { reason: 'révocation admin' })
    await load()
  } catch (e) {
    error.value = errorText(e)
  } finally {
    busy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <div class="crumbs"><NuxtLink to="/admin">Administration</NuxtLink> › Télémétrie</div>
        <h1>Agents de télémétrie</h1>
        <p class="small muted">
          Enrôlement mTLS (F23). L'identité du certificat fixe le projet ; un projet déclaré
          différent est refusé. Quotas et rétention par projet.
        </p>
      </div>
    </div>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>
    <div v-if="notice" class="banner ok" role="status" data-testid="telemetry-enroll-notice">{{ notice }}</div>

    <section v-if="isAdmin" class="card" aria-labelledby="enroll-title" data-testid="telemetry-enroll">
      <h2 id="enroll-title">Enrôler un agent</h2>
      <form class="actions" style="flex-wrap: wrap" @submit.prevent="enroll">
        <div class="field">
          <label for="tel-slug">Slug</label>
          <input id="tel-slug" v-model="form.slug" required pattern="[a-z][a-z0-9-]{1,40}" data-testid="tel-slug" />
        </div>
        <div class="field" style="flex: 1">
          <label for="tel-name">Nom</label>
          <input id="tel-name" v-model="form.name" required maxlength="120" data-testid="tel-name" />
        </div>
        <div class="field">
          <label for="tel-project">Projet</label>
          <input id="tel-project" v-model="form.project" required data-testid="tel-project" />
        </div>
        <button type="submit" class="primary" :disabled="busy" data-testid="tel-enroll">Enrôler</button>
      </form>
      <pre v-if="lastCert" class="small" data-testid="tel-cert-once">empreinte {{ lastCert.fingerprint_sha256 }}</pre>
    </section>

    <section class="card" aria-labelledby="agents-title">
      <h2 id="agents-title">Agents</h2>
      <p v-if="!agents.length" class="empty">Aucun agent enrôlé.</p>
      <table v-else data-testid="telemetry-agents">
        <thead>
          <tr>
            <th scope="col">Slug</th>
            <th scope="col">Projet</th>
            <th scope="col">État</th>
            <th scope="col">Empreinte</th>
            <th scope="col"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in agents" :key="a.id">
            <td>{{ a.slug }}</td>
            <td>{{ a.project_slug }}</td>
            <td><span class="badge">{{ a.status }}</span></td>
            <td class="small muted">{{ a.cert_fingerprint_sha256.slice(0, 12) }}…</td>
            <td>
              <button
                v-if="isAdmin && a.status !== 'revoked'"
                type="button"
                class="button"
                :disabled="busy"
                @click="revoke(a.id)"
              >Révoquer</button>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>
