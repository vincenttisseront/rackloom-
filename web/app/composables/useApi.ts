// Client de l'API Rackloom. Toute décision (droits, actions disponibles, valeurs techniques) reste côté serveur :
// l'interface n'affiche que ce que l'API renvoie et rejoue les refus tels quels.

export interface ApiErrorBody {
  code?: string
  message?: string
  field?: string
  details?: Record<string, unknown>
}

export class ApiError extends Error {
  status: number
  code: string
  field?: string
  details?: Record<string, unknown>

  constructor(status: number, body: ApiErrorBody | null) {
    super(body?.message || `Erreur ${status}`)
    this.status = status
    this.code = body?.code || 'error'
    this.field = body?.field
    this.details = body?.details
  }
}

export interface SessionInfo {
  user: { id: string; email: string; display_name: string }
  roles: { role: string; project_id: string | null; environment_id: string | null }[]
  csrf_token: string
}

export const useSession = () => useState<SessionInfo | null>('session', () => null)

function newKey(): string {
  return crypto.randomUUID()
}

export function useApi() {
  const session = useSession()

  async function request<T = any>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = { Accept: 'application/json' }
    if (body !== undefined) headers['Content-Type'] = 'application/json'
    if (method !== 'GET') {
      if (session.value?.csrf_token) headers['X-CSRF-Token'] = session.value.csrf_token
      headers['Idempotency-Key'] = newKey()
    }
    const res = await fetch(`/api/v1${path}`, {
      method,
      headers,
      credentials: 'same-origin',
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    if (res.status === 204) return null as T
    const data = await res.json().catch(() => null)
    if (!res.ok) {
      if (res.status === 401 && path !== '/auth/login' && path !== '/session') {
        session.value = null
        await navigateTo('/connexion')
      }
      throw new ApiError(res.status, data)
    }
    return data as T
  }

  return {
    get: <T = any>(path: string) => request<T>('GET', path),
    post: <T = any>(path: string, body: unknown = {}) => request<T>('POST', path, body),
    put: <T = any>(path: string, body: unknown = {}) => request<T>('PUT', path, body),
  }
}

export function hasRole(session: SessionInfo | null, role: string, projectId?: string): boolean {
  if (!session) return false
  return session.roles.some(r => r.role === role && (projectId === undefined || r.project_id === null || r.project_id === projectId))
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.message
  return 'Erreur inattendue. Réessayez ou rechargez la page.'
}
