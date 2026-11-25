<script setup lang="ts">
const session = useSession()
const api = useApi()
const route = useRoute()

const isAdmin = computed(() => hasRole(session.value, 'platform_admin'))
const isPublisher = computed(() => hasRole(session.value, 'template_publisher'))
const isAuthor = computed(() => hasRole(session.value, 'template_author'))
const isAuditor = computed(() => hasRole(session.value, 'auditor'))

async function logout() {
  try {
    await api.post('/auth/logout')
  } finally {
    session.value = null
    await navigateTo('/connexion')
  }
}
</script>

<template>
  <div>
    <a class="skip-link" href="#contenu">Aller au contenu</a>
    <header class="topbar">
      <div class="topbar-inner">
        <NuxtLink to="/" class="brand">Rackloom</NuxtLink>
        <nav v-if="session" aria-label="Navigation principale">
          <NuxtLink to="/">Accueil</NuxtLink>
          <NuxtLink v-if="isAuthor || isPublisher || isAdmin" to="/atelier">Atelier</NuxtLink>
          <NuxtLink v-if="isPublisher" to="/admin/templates">Administration › Templates</NuxtLink>
          <NuxtLink v-if="isAdmin" to="/admin/connecteurs">Administration › Connecteurs</NuxtLink>
          <NuxtLink v-if="isAdmin" to="/admin/environnements">Administration › Environnements</NuxtLink>
          <NuxtLink v-if="isAuditor || isAdmin" to="/audit">Audit</NuxtLink>
        </nav>
        <div v-if="session" class="who">
          <span>{{ session.user.display_name }}</span>
          <button type="button" @click="logout">Se déconnecter</button>
        </div>
      </div>
    </header>
    <main id="contenu" :key="route.path" tabindex="-1">
      <slot />
    </main>
  </div>
</template>
