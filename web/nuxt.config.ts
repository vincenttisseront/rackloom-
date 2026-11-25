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
  // SPA : seules les coquilles « / », 200 et 404 sont générées. Le parcours des liens prérendait chaque
  // route et deux écritures concurrentes de index.html ont produit un fichier corrompu.
  nitro: { prerender: { crawlLinks: false } },
  typescript: { strict: true },
})
