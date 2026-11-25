// Construit l'en-tête Content-Security-Policy de la SPA : les seuls scripts en ligne autorisés sont ceux
// générés par Nuxt (importmap, configuration), identifiés par leur empreinte SHA-256 à la construction.
import { createHash } from 'node:crypto'
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

const root = process.argv[2] || '.output/public'
const out = process.argv[3] || 'csp.caddy'
const hashes = new Set()

function walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) walk(path)
    else if (name.endsWith('.html')) {
      const html = readFileSync(path, 'utf8')
      if (html.split('</html>').length !== 2 || !html.trimEnd().endsWith('</html>')) {
        throw new Error(`HTML généré corrompu : ${path}`)
      }
      for (const m of html.matchAll(/<script(?![^>]*\bsrc=)([^>]*)>([\s\S]*?)<\/script>/g)) {
        if (/type="application\/json"/.test(m[1]) || !m[2].trim()) continue
        hashes.add(`'sha256-${createHash('sha256').update(m[2], 'utf8').digest('base64')}'`)
      }
    }
  }
}
walk(root)

const policy = [
  "default-src 'self'",
  `script-src 'self' ${[...hashes].sort().join(' ')}`.trim(),
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "connect-src 'self'",
  "font-src 'self'",
  "frame-ancestors 'none'",
  "base-uri 'none'",
  "form-action 'self'",
  "object-src 'none'",
].join('; ')
writeFileSync(out, `header Content-Security-Policy "${policy}"\n`)
console.log(`${hashes.size} script(s) en ligne autorisé(s) par empreinte`)
