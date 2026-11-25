// WebUI Rackloom : SPA statique servie par Caddy sur la même origine que l'API (/api/v1).
export default defineNuxtConfig({
  compatibilityDate: '2026-11-01',
  ssr: false,
  devtools: { enabled: false },
  telemetry: false,
  css: ['~/assets/main.css'],
  app: {
    head: {
      htmlAttrs: { lang: 'fr' },
      title: 'Rackloom',
      meta: [
        { name: 'viewport', content: 'width=device-width, initial-scale=1' },
        { name: 'referrer', content: 'no-referrer' },
      ],
    },
  },
  experimental: { payloadExtraction: false },
  typescript: { strict: true },
})
