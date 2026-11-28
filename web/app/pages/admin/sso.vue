<script setup lang="ts">
const api = useApi()
const error = ref<string | null>(null)
const notice = ref<string | null>(null)
const cfg = ref<any>(null)
const form = reactive({
  enabled: false,
  issuer_url: '',
  client_id: '',
  client_secret: '',
  clear_client_secret: false,
  step_up_acr: 'urn:rackloom:step-up',
})
const mapForm = reactive({
  idp_group: '',
  role: 'approver',
  project_id: '',
})

async function load() {
  error.value = null
  cfg.value = await api.get('/admin/oidc')
  form.enabled = !!cfg.value.enabled
  form.issuer_url = cfg.value.issuer_url || ''
  form.client_id = cfg.value.client_id || ''
  form.step_up_acr = cfg.value.step_up_acr || 'urn:rackloom:step-up'
  form.client_secret = ''
  form.clear_client_secret = false
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    error.value = errorText(e)
  }
})

async function save() {
  error.value = null
  notice.value = null
  try {
    const body: Record<string, unknown> = {
      enabled: form.enabled,
      issuer_url: form.issuer_url.trim(),
      client_id: form.client_id.trim(),
      step_up_acr: form.step_up_acr.trim(),
      clear_client_secret: form.clear_client_secret,
    }
    if (form.client_secret.trim()) body.client_secret = form.client_secret.trim()
    cfg.value = await api.put('/admin/oidc', body)
    form.client_secret = ''
    notice.value = 'Configuration OIDC enregistrée (secret jamais réaffiché).'
  } catch (e) {
    error.value = errorText(e)
  }
}

async function probe() {
  error.value = null
  notice.value = null
  try {
    const r = await api.post('/admin/oidc/probe', {}) as any
    notice.value = r.reachable ? 'IdP joignable (discovery OK).' : `IdP injoignable (${r.code}).`
    await load()
  } catch (e) {
    error.value = errorText(e)
  }
}

async function addMapping() {
  error.value = null
  try {
    cfg.value = await api.post('/admin/oidc/mappings', {
      idp_group: mapForm.idp_group.trim(),
      role: mapForm.role,
      project_id: mapForm.project_id.trim() || null,
    })
    mapForm.idp_group = ''
  } catch (e) {
    error.value = errorText(e)
  }
}

async function removeMapping(id: string) {
  error.value = null
  try {
    cfg.value = await api.del(`/admin/oidc/mappings/${id}`)
  } catch (e) {
    error.value = errorText(e)
  }
}
</script>

<template>
  <div>
    <div class="page-head">
      <div>
        <h1>Administration › SSO OIDC</h1>
        <p class="small muted">
          Lot 4 : issuer, client (PKCE), mapping groupes → rôles. Le secret client n’est jamais relu.
          IdP d’entreprise : non vérifié ; Dex local pour le laboratoire.
        </p>
      </div>
    </div>
    <div v-if="error" class="banner bad" role="alert">{{ error }}</div>
    <div v-if="notice" class="banner ok" role="status">{{ notice }}</div>

    <section v-if="cfg" class="card" aria-labelledby="oidc-cfg" data-testid="oidc-config">
      <h2 id="oidc-cfg">Configuration</h2>
      <dl class="small">
        <dt>Secret client</dt>
        <dd data-testid="oidc-secret-status">{{ cfg.client_secret_configured ? 'défini' : 'absent (client public)' }}</dd>
        <dt>SSO disponible</dt>
        <dd>{{ cfg.sso_available ? 'oui' : 'non' }}</dd>
        <dt>IdP joignable</dt>
        <dd data-testid="oidc-reachable">{{ cfg.idp_reachable == null ? '—' : (cfg.idp_reachable ? 'oui' : 'non') }}</dd>
      </dl>
      <label class="field">
        <input v-model="form.enabled" type="checkbox" data-testid="oidc-enabled" />
        Activer OIDC
      </label>
      <label class="field">
        Issuer URL
        <input v-model="form.issuer_url" class="mono" data-testid="oidc-issuer" autocomplete="off" />
      </label>
      <label class="field">
        Client ID
        <input v-model="form.client_id" class="mono" data-testid="oidc-client-id" autocomplete="off" />
      </label>
      <label class="field">
        Client secret (optionnel, écriture seule)
        <input v-model="form.client_secret" type="password" data-testid="oidc-client-secret" autocomplete="new-password" />
      </label>
      <label class="field">
        <input v-model="form.clear_client_secret" type="checkbox" />
        Effacer le secret client
      </label>
      <label class="field">
        ACR step-up
        <input v-model="form.step_up_acr" class="mono" data-testid="oidc-step-up-acr" />
      </label>
      <div class="row">
        <button type="button" class="primary" data-testid="oidc-save" @click="save">Enregistrer</button>
        <button type="button" data-testid="oidc-probe" @click="probe">Tester l’IdP</button>
      </div>
    </section>

    <section v-if="cfg" class="card" aria-labelledby="oidc-map" data-testid="oidc-mappings">
      <h2 id="oidc-map">Mapping groupes → rôles</h2>
      <ul class="small">
        <li v-for="m in cfg.mappings" :key="m.id">
          <code>{{ m.idp_group }}</code> → {{ m.role }}
          <template v-if="m.project_id"> (projet {{ m.project_id.slice(0, 8) }}…)</template>
          <button type="button" class="linkish" @click="removeMapping(m.id)">Retirer</button>
        </li>
      </ul>
      <label class="field">
        Groupe IdP
        <input v-model="mapForm.idp_group" data-testid="oidc-map-group" />
      </label>
      <label class="field">
        Rôle
        <select v-model="mapForm.role" data-testid="oidc-map-role">
          <option>platform_admin</option>
          <option>approver</option>
          <option>operator</option>
          <option>auditor</option>
          <option>project_admin</option>
          <option>connector_admin</option>
          <option>template_publisher</option>
        </select>
      </label>
      <label class="field">
        Project ID (si rôle projet)
        <input v-model="mapForm.project_id" class="mono" data-testid="oidc-map-project" />
      </label>
      <button type="button" data-testid="oidc-map-add" @click="addMapping">Ajouter</button>
    </section>
  </div>
</template>
