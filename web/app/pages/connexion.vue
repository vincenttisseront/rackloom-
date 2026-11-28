<script setup lang="ts">
const api = useApi()
const session = useSession()
const route = useRoute()
const email = ref('')
const password = ref('')
const error = ref<string | null>(null)
const busy = ref(false)
const oidc = ref<{ enabled: boolean; sso_available: boolean; login_path: string } | null>(null)

onMounted(async () => {
  try {
    oidc.value = await api.get('/auth/oidc')
  } catch {
    oidc.value = null
  }
  const err = typeof route.query.oidc_error === 'string' ? route.query.oidc_error : null
  if (err) error.value = `Connexion SSO refusée (${err}).`
})

async function login() {
  busy.value = true
  error.value = null
  try {
    await api.post('/auth/login', { email: email.value.trim(), password: password.value })
    session.value = await api.get<SessionInfo>('/session')
    const next = typeof route.query.suite === 'string' && route.query.suite.startsWith('/') && !route.query.suite.startsWith('//') ? route.query.suite : '/'
    await navigateTo(next)
  } catch (e) {
    error.value = e instanceof ApiError && e.status === 401 ? 'Identifiants incorrects.' : errorText(e)
  } finally {
    busy.value = false
    password.value = ''
  }
}

function ssoLogin() {
  const suite = typeof route.query.suite === 'string' ? route.query.suite : '/'
  const q = suite.startsWith('/') && !suite.startsWith('//') ? `?return_to=${encodeURIComponent(suite)}` : ''
  window.location.href = `/api/v1/auth/oidc/login${q}`
}
</script>

<template>
  <div class="login">
    <form class="card" aria-labelledby="login-title" @submit.prevent="login">
      <h1 id="login-title">Connexion</h1>
      <p class="small muted">
        Démonstration locale : identités en <code>@demo.invalid</code>, mots de passe générés à l'installation
        (<code>.lab/demo-credentials.txt</code>). SSO OIDC : IdP Dex local (mot de passe IdP
        <code>rackloom-oidc-lab</code>).
      </p>
      <div v-if="error" class="banner bad" role="alert">{{ error }}</div>
      <div
        v-if="oidc?.enabled && !oidc.sso_available"
        class="banner warn"
        role="status"
        data-testid="sso-down-banner"
      >
        SSO indisponible. Utilisez un compte local autorisé par une intervention d'urgence (F14),
        ou contactez un opérateur console.
      </div>
      <div v-if="oidc?.enabled && oidc.sso_available" class="field">
        <button type="button" class="primary" data-testid="oidc-login" @click="ssoLogin">
          Se connecter avec SSO
        </button>
      </div>
      <div class="field">
        <label for="email">Adresse e-mail</label>
        <input id="email" v-model="email" type="email" autocomplete="username" required />
      </div>
      <div class="field">
        <label for="password">Mot de passe</label>
        <input id="password" v-model="password" type="password" autocomplete="current-password" required />
      </div>
      <button type="submit" class="primary" :disabled="busy" data-testid="local-login">Se connecter</button>
    </form>
  </div>
</template>
