export default defineNuxtRouteMiddleware(async (to) => {
  const session = useSession()
  if (session.value === null) {
    try {
      session.value = await useApi().get<SessionInfo>('/session')
    } catch {
      session.value = null
    }
  }
  if (!session.value && to.path !== '/connexion') {
    return navigateTo({ path: '/connexion', query: to.fullPath !== '/' ? { suite: to.fullPath } : {} })
  }
  if (session.value && to.path === '/connexion') return navigateTo('/')
})
