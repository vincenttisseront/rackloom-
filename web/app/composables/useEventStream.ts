// Flux SSE lu avec fetch (les types d'événements sont ouverts) ; reprise par Last-Event-ID, sans doublon.

export interface RackloomEvent {
  id: number
  type: string
  source: string
  severity: string
  occurred_at: string
  job_id: string | null
  data: Record<string, any>
}

export function useEventStream(path: string, onEvent: (e: RackloomEvent) => void, initialAfter = 0) {
  let lastId = initialAfter
  let stopped = false
  let controller: AbortController | null = null
  const connected = ref(false)
  const ended = ref(false)

  async function once(): Promise<void> {
    controller = new AbortController()
    const res = await fetch(`/api/v1${path}`, {
      headers: { Accept: 'text/event-stream', 'Last-Event-ID': String(lastId) },
      credentials: 'same-origin',
      signal: controller.signal,
    })
    if (res.status === 401 || res.status === 403 || res.status === 404) {
      ended.value = true
      stopped = true
      return
    }
    if (!res.ok || !res.body) throw new Error(`flux ${res.status}`)
    connected.value = true
    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    while (!stopped) {
      const { value, done } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      let idx
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const chunk = buffer.slice(0, idx)
        buffer = buffer.slice(idx + 2)
        let type = 'message'
        let data = ''
        let id: number | null = null
        for (const line of chunk.split('\n')) {
          if (line.startsWith('event: ')) type = line.slice(7)
          else if (line.startsWith('data: ')) data += line.slice(6)
          else if (line.startsWith('id: ')) id = Number(line.slice(4))
        }
        if (type === 'session.ended') {
          ended.value = true
          stopped = true
          return
        }
        if (!data || id === null || id <= lastId) continue
        lastId = id
        try {
          onEvent(JSON.parse(data))
        } catch {
          // Donnée non fiable mal formée : ignorée, jamais interprétée.
        }
      }
    }
  }

  async function loop() {
    while (!stopped) {
      try {
        await once()
      } catch {
        // coupure réseau : reconnexion ci-dessous
      }
      connected.value = false
      if (!stopped) await new Promise(r => setTimeout(r, 1500))
    }
  }

  function start() {
    stopped = false
    loop()
  }
  function stop() {
    stopped = true
    controller?.abort()
  }
  return { start, stop, connected, ended, setAfter: (n: number) => { lastId = Math.max(lastId, n) } }
}
