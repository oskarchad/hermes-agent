/**
 * Shared HTTP transport policy for the Electron main process's Hermes REST
 * helpers (fetchJson / fetchPublicJson / downloadViaTokenToFile).
 *
 * Two concerns live here so they can be unit-tested without Electron:
 *
 * 1. Connection-pooled keep-alive agents. Opening a fresh TCP socket per call
 *    is what produced the burst-traffic ECONNRESET storms (#92976): the
 *    backend closes idle keep-alive sockets and the next write on a reused
 *    raw socket dies with 'socket hang up'. JSON calls and streaming
 *    downloads get SEPARATE pools so a handful of long-lived download
 *    streams can never starve the small, latency-sensitive JSON calls out of
 *    the socket pool.
 *
 * 2. A retry policy that is safe for non-idempotent verbs. A transient
 *    transport error does NOT mean the server didn't process the request —
 *    an ECONNRESET can arrive after the backend already handled a POST
 *    (created the session, submitted the prompt) and merely lost the socket
 *    before the response was read. Blindly retrying every verb double-submits.
 *
 *    The rule implemented by shouldRetryRequest():
 *      - Idempotent verbs (GET / HEAD / OPTIONS) retry on any transient
 *        transport error — replaying them is harmless by definition.
 *      - Non-idempotent verbs (POST / PUT / PATCH / DELETE) retry ONLY when
 *        the request provably never reached the server:
 *          a) connection-establishment failures (ECONNREFUSED, ENOTFOUND,
 *             EAI_AGAIN, EHOSTUNREACH, ENETUNREACH) — no connection means no
 *             request; or
 *          b) a transient error thrown before we started flushing the
 *             request (requestState.bodySent === false).
 *        Anything ambiguous — ECONNRESET / EPIPE / 'socket hang up' after
 *        the body went out — is NOT retried; the error surfaces to the
 *        caller. When in doubt, don't retry a non-idempotent request.
 */

import http from 'node:http'
import https from 'node:https'

import { attachPrematureResponseGuard } from './backend-health'
import { DEFAULT_FETCH_TIMEOUT_MS, resolveTimeoutMs } from './hardening'

// JSON pool: many small concurrent calls (session lists, config, prompts).
const HTTP_JSON_AGENT = new http.Agent({ keepAlive: true, maxSockets: 50 })
const HTTPS_JSON_AGENT = new https.Agent({ keepAlive: true, maxSockets: 50 })

// Download pool: few long-lived streaming bodies. Isolated from the JSON pool
// so saturating it with large file downloads can't block interactive calls.
const HTTP_DOWNLOAD_AGENT = new http.Agent({ keepAlive: true, maxSockets: 8 })
const HTTPS_DOWNLOAD_AGENT = new https.Agent({ keepAlive: true, maxSockets: 8 })

function jsonAgentFor(protocol) {
  return protocol === 'https:' ? HTTPS_JSON_AGENT : HTTP_JSON_AGENT
}

function downloadAgentFor(protocol) {
  return protocol === 'https:' ? HTTPS_DOWNLOAD_AGENT : HTTP_DOWNLOAD_AGENT
}

// Close pooled sockets so lingering keep-alive connections can't hold the
// process open (or leak FDs) across quit. Wired to app 'will-quit' in main.ts.
function destroyKeepaliveAgents() {
  for (const agent of [HTTP_JSON_AGENT, HTTPS_JSON_AGENT, HTTP_DOWNLOAD_AGENT, HTTPS_DOWNLOAD_AGENT]) {
    agent.destroy()
  }
}

// Transient transport errors: retry MAY be safe (subject to verb gating).
const TRANSIENT_CODES = new Set([
  'ECONNRESET',
  'ECONNREFUSED',
  'EPIPE',
  'ETIMEDOUT',
  'EAI_AGAIN',
  'ENOTFOUND',
  'EHOSTUNREACH',
  'ENETUNREACH'
])

// Errors that prove the request never reached the server: the TCP connection
// (or name resolution) failed outright, so nothing was submitted.
const NEVER_SENT_CODES = new Set(['ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'EHOSTUNREACH', 'ENETUNREACH'])

const IDEMPOTENT_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

function isIdempotentMethod(method) {
  return IDEMPOTENT_METHODS.has(String(method || 'GET').toUpperCase())
}

function isTransientTransportError(error) {
  if (!error) {
    return false
  }

  if (TRANSIENT_CODES.has(error.code)) {
    return true
  }

  const msg = String(error.message || '')

  return msg.includes('socket hang up') || msg.includes('read ECONNRESET')
}

/**
 * The verb-gated retry decision.
 *
 * @param error        the transport error from the failed attempt
 * @param method       HTTP verb of the request ('GET', 'POST', ...)
 * @param requestState per-attempt state; requestState.bodySent is set true by
 *                     the caller just BEFORE the first byte of the request is
 *                     flushed, so a `false` here proves nothing went out.
 */
function shouldRetryRequest(error, method, requestState: any = {}) {
  if (!isTransientTransportError(error)) {
    return false
  }

  if (isIdempotentMethod(method)) {
    return true
  }

  // Non-idempotent: only when the request provably never reached the server.
  if (NEVER_SENT_CODES.has(error && error.code)) {
    return true
  }

  if (requestState.bodySent === false) {
    return true
  }

  // Ambiguous (reset/hang-up after the body was flushed): the server may have
  // processed it. Surface the error rather than risk a double submit.
  return false
}

/**
 * Run `makeAttempt` with bounded retries under the policy above.
 *
 * `makeAttempt(requestState)` must return a Promise and should set
 * `requestState.bodySent = true` immediately before flushing the request
 * (before the first req.write()/req.end()). Each attempt gets a fresh state
 * object initialized to { bodySent: false }.
 */
async function withRetry(makeAttempt, options: any = {}) {
  const method = String(options.method || 'GET').toUpperCase()
  const maxRetries = Number.isInteger(options.maxRetries) ? options.maxRetries : 2

  const delayFn =
    options.delayFn || (attempt => new Promise(r => setTimeout(r, Math.min(200 * Math.pow(2, attempt), 2000))))

  let lastError

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    const requestState = { bodySent: false }

    try {
      return await makeAttempt(requestState)
    } catch (error) {
      lastError = error

      if (attempt < maxRetries && shouldRetryRequest(error, method, requestState)) {
        await delayFn(attempt)

        continue
      }

      throw error
    }
  }

  throw lastError
}

interface PublicJsonRequestOptions {
  body?: unknown
  headers?: Record<string, string>
  method?: string
  timeoutMs?: number
}

type RemoteHeadersResolver = (url: string) => Record<string, string>

/**
 * Build the credential-free JSON transport used by the Electron main process.
 * The injected resolver keeps connection-specific header authority in main.ts
 * while making the exact HTTP path executable without booting Electron.
 */
function createPublicJsonTransport(headersForRequest: RemoteHeadersResolver = () => ({})) {
  return function fetchPublicJson(url: string, options: PublicJsonRequestOptions = {}) {
    return withRetry(
      (requestState: any) =>
        new Promise((resolve, reject) => {
          const body = options.body === undefined ? undefined : Buffer.from(JSON.stringify(options.body))
          let settled = false

          let cleanupResponseGuard = () => {}

          const resolveOnce = (value: unknown) => {
            if (settled) {
              return
            }

            settled = true
            cleanupResponseGuard()
            resolve(value)
          }

          const rejectOnce = (error: Error) => {
            if (settled) {
              return
            }

            settled = true
            cleanupResponseGuard()
            reject(error)
          }

          let parsed

          try {
            parsed = new URL(url)
          } catch (error: any) {
            rejectOnce(new Error(`Invalid URL: ${error.message}`))

            return
          }

          const client = parsed.protocol === 'https:' ? https : http
          const agent = jsonAgentFor(parsed.protocol)
          const timeoutMs = resolveTimeoutMs(options.timeoutMs, DEFAULT_FETCH_TIMEOUT_MS)

          if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
            rejectOnce(new Error(`Unsupported Hermes backend URL protocol: ${parsed.protocol}`))

            return
          }

          const req = client.request(
            parsed,
            {
              agent,
              method: options.method || 'GET',
              headers: {
                ...headersForRequest(url),
                ...(options.headers || {}),
                'Content-Type': 'application/json',
                ...(body ? { 'Content-Length': String(body.length) } : {})
              }
            },
            res => {
              const chunks: Buffer[] = []
              cleanupResponseGuard = attachPrematureResponseGuard(res, rejectOnce, url)
              res.once('error', rejectOnce)
              res.on('data', chunk => chunks.push(chunk))
              res.once('end', () => {
                const text = Buffer.concat(chunks).toString('utf8')

                if ((res.statusCode || 500) >= 400) {
                  rejectOnce(new Error(`${res.statusCode}: ${text || res.statusMessage}`))

                  return
                }

                if (!text) {
                  resolveOnce(null)

                  return
                }

                const looksHtml = /^\s*<(?:!doctype|html)/i.test(text)
                const contentType = String(res.headers['content-type'] || '')

                if (looksHtml || contentType.includes('text/html')) {
                  rejectOnce(
                    new Error(
                      `Expected JSON from ${url} but got HTML (status ${res.statusCode}). ` +
                        'The endpoint is likely missing on the Hermes backend.'
                    )
                  )

                  return
                }

                try {
                  resolveOnce(JSON.parse(text))
                } catch {
                  rejectOnce(new Error(`Invalid JSON from ${url} (status ${res.statusCode}): ${text.slice(0, 200)}`))
                }
              })
            }
          )

          req.once('error', rejectOnce)
          req.setTimeout(timeoutMs, () => {
            req.destroy(new Error(`Timed out connecting to Hermes backend after ${timeoutMs}ms`))
          })

          requestState.bodySent = true

          if (body) {
            req.write(body)
          }

          req.end()
        }),
      { method: options.method || 'GET' }
    )
  }
}

export {
  createPublicJsonTransport,
  destroyKeepaliveAgents,
  downloadAgentFor,
  isIdempotentMethod,
  isTransientTransportError,
  jsonAgentFor,
  shouldRetryRequest,
  withRetry
}
