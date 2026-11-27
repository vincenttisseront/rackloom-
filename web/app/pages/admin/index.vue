<script setup lang="ts">
// Hub Administration : /admin n'avait pas de page (404 Nuxt), alors que la section
// produit s'appelle « Administration ». Les sous-pages restent les routes canoniques.
const session = useSession()

const isAdmin = computed(() => hasRole(session.value, 'platform_admin'))
const isPublisher = computed(() => hasRole(session.value, 'template_publisher'))
const canConnectors = computed(() =>
  isAdmin.value
  || hasRole(session.value, 'connector_admin')
  || hasRole(session.value, 'project_admin'),
)

const links = computed(() => {
  const items: { to: string; title: string; blurb: string }[] = []
  if (isPublisher.value) {
    items.push({
      to: '/admin/templates',
      title: 'Templates',
      blurb: 'Versions publiées, contrats et impact avant publication.',
    })
  }
  if (canConnectors.value) {
    items.push({
      to: '/admin/connecteurs',
      title: 'Connecteurs',
      blurb: 'Configuration, matrice de capacités, assistant (écriture seule des secrets).',
    })
  }
  if (isAdmin.value) {
    items.push(
      {
        to: '/admin/environnements',
        title: 'Environnements',
        blurb: 'Politiques et gel d’urgence par environnement.',
      },
      {
        to: '/admin/gouvernance',
        title: 'Gouvernance',
        blurb: 'Bundle de politiques, exceptions, droits temporaires, accès d’urgence.',
      },
      {
        to: '/admin/identites',
        title: 'Identités',
        blurb: 'SCIM, revue d’accès, copie d’audit, alertes certificats TLS.',
      },
    )
  }
  return items
})
</script>

<template>
  <div class="page">
    <h1>Administration</h1>
    <p class="lead">
      Section réservée aux rôles habilités. Les droits sont vérifiés à chaque appel API ;
      masquer un lien n’est pas un contrôle.
    </p>
    <p v-if="!links.length" class="empty" data-testid="admin-empty" role="status">
      Aucun écran d’administration n’est disponible pour votre rôle.
    </p>
    <ul v-else class="hub" data-testid="admin-hub">
      <li v-for="item in links" :key="item.to">
        <NuxtLink :to="item.to" :data-testid="`admin-link-${item.to.split('/').pop()}`">
          <span class="title">{{ item.title }}</span>
          <span class="blurb">{{ item.blurb }}</span>
        </NuxtLink>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.page { max-width: 40rem; margin: 0 auto; padding: 1.5rem 1rem 3rem; }
.lead { color: var(--muted, #444); margin-bottom: 1.5rem; }
.empty { color: var(--muted, #666); }
.hub { list-style: none; padding: 0; margin: 0; display: grid; gap: 0.75rem; }
.hub a {
  display: block;
  padding: 0.85rem 1rem;
  border: 1px solid #ddd;
  border-radius: 4px;
  text-decoration: none;
  color: inherit;
}
.hub a:hover { border-color: #888; }
.title { display: block; font-weight: 600; }
.blurb { display: block; margin-top: 0.25rem; color: var(--muted, #555); font-size: 0.95rem; }
</style>
