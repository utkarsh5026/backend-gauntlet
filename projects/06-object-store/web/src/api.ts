// Thin client for the project-06 path-style S3 API.
//
// All requests go through the Vite proxy prefix `/s3` (see vite.config.ts),
// which strips the prefix and forwards to the Rust backend (default :9006). That
// keeps the browser same-origin, so no CORS layer is needed on the backend and we
// can freely read the `ETag` response header the store returns.
//
// The endpoints map 1:1 onto src/routes.rs:
//   PUT    /{bucket}                                   create bucket
//   GET    /{bucket}?prefix&delimiter&max-keys&…       ListObjectsV2 (JSON)
//   PUT    /{bucket}/{key}                             single-shot object PUT (V2)
//   GET    /{bucket}/{key}                             download
//   DELETE /{bucket}/{key}                             delete
//   POST   /{bucket}/{key}?uploads                     InitiateMultipartUpload (V4)
//   PUT    /{bucket}/{key}?uploadId&partNumber         UploadPart (V4)
//   POST   /{bucket}/{key}?uploadId                    CompleteMultipartUpload (V4)
//   DELETE /{bucket}/{key}?uploadId                    AbortMultipartUpload (V4)

export const BASE = '/s3'

export interface S3Object {
  key: string
  size: number
  etag: string
  lastModified: string
}

export interface Listing {
  name: string
  prefix: string
  objects: S3Object[]
  commonPrefixes: string[]
  isTruncated: boolean
  nextContinuationToken: string | null
}

export interface InitiateResult {
  bucket: string
  key: string
  uploadId: string
}

export interface CompletedPart {
  partNumber: number
  etag: string
}

export interface XhrResult {
  status: number
  ok: boolean
  etag: string | null
  text: string
}

export class ApiError extends Error {
  status: number
  body: string
  constructor(status: number, body: string) {
    super(status === 0 ? body : `HTTP ${status}${body ? ` — ${body}` : ''}`)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

/** Encode a key while preserving `/` — the store treats keys as flat strings,
 *  but the route is `/{bucket}/{*key}`, so slashes must stay path separators. */
function encodeKey(key: string): string {
  return key.split('/').map(encodeURIComponent).join('/')
}

export function objectUrl(bucket: string, key: string): string {
  return `${BASE}/${encodeURIComponent(bucket)}/${encodeKey(key)}`
}

async function expectOk(res: Response): Promise<Response> {
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new ApiError(res.status, body.slice(0, 500))
  }
  return res
}

async function fetchOk(method: string, url: string, init?: RequestInit): Promise<Response> {
  let res: Response
  try {
    res = await fetch(url, { method, ...init })
  } catch {
    throw new ApiError(0, 'network error — is the object store running?')
  }
  return expectOk(res)
}

export async function health(): Promise<boolean> {
  try {
    const res = await fetch(`${BASE}/healthz`)
    return res.ok
  } catch {
    return false
  }
}

export async function createBucket(bucket: string): Promise<void> {
  await fetchOk('PUT', `${BASE}/${encodeURIComponent(bucket)}`)
}

export async function listObjects(
  bucket: string,
  opts: {
    prefix?: string
    delimiter?: string
    continuationToken?: string
    maxKeys?: number
  } = {},
): Promise<Listing> {
  const q = new URLSearchParams()
  if (opts.prefix) q.set('prefix', opts.prefix)
  if (opts.delimiter) q.set('delimiter', opts.delimiter)
  if (opts.continuationToken) q.set('continuation-token', opts.continuationToken)
  if (opts.maxKeys) q.set('max-keys', String(opts.maxKeys))
  const qs = q.toString()
  const res = await fetchOk('GET', `${BASE}/${encodeURIComponent(bucket)}${qs ? `?${qs}` : ''}`)
  return res.json()
}

export async function deleteObject(bucket: string, key: string): Promise<void> {
  await fetchOk('DELETE', objectUrl(bucket, key))
}

// ── Uploads use XHR (not fetch) so we get real upload-progress events ─────────

export function xhrUpload(
  method: string,
  url: string,
  body: Blob | File,
  opts: { contentType?: string; onProgress?: (loaded: number, total: number) => void } = {},
): Promise<XhrResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open(method, url)
    if (opts.contentType) xhr.setRequestHeader('Content-Type', opts.contentType)
    if (opts.onProgress) {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) opts.onProgress!(e.loaded, e.total)
      }
    }
    xhr.onload = () => {
      const ok = xhr.status >= 200 && xhr.status < 300
      const result: XhrResult = {
        status: xhr.status,
        ok,
        etag: xhr.getResponseHeader('etag'),
        text: xhr.responseText,
      }
      if (ok) resolve(result)
      else reject(new ApiError(xhr.status, (xhr.responseText || '').slice(0, 500)))
    }
    xhr.onerror = () => reject(new ApiError(0, 'network error — is the object store running?'))
    xhr.send(body)
  })
}

export function putObject(
  bucket: string,
  key: string,
  file: File,
  onProgress?: (loaded: number, total: number) => void,
): Promise<XhrResult> {
  return xhrUpload('PUT', objectUrl(bucket, key), file, {
    contentType: file.type || 'application/octet-stream',
    onProgress,
  })
}

// ── Multipart (V4) ────────────────────────────────────────────────────────────

export async function initiateMultipart(
  bucket: string,
  key: string,
  contentType: string,
): Promise<InitiateResult> {
  const res = await fetchOk('POST', `${objectUrl(bucket, key)}?uploads`, {
    headers: { 'Content-Type': contentType },
  })
  return res.json()
}

export function uploadPart(
  bucket: string,
  key: string,
  uploadId: string,
  partNumber: number,
  blob: Blob,
  onProgress?: (loaded: number, total: number) => void,
): Promise<XhrResult> {
  const url = `${objectUrl(bucket, key)}?uploadId=${encodeURIComponent(uploadId)}&partNumber=${partNumber}`
  return xhrUpload('PUT', url, blob, { onProgress })
}

export async function completeMultipart(
  bucket: string,
  key: string,
  uploadId: string,
  parts: CompletedPart[],
): Promise<{ bucket?: string; key?: string; etag?: string }> {
  // Send the ordered part list as JSON. The backend's route currently ignores
  // the body (placeholder), but this is the shape the CompleteMultipartUpload
  // handler is meant to parse once V4/protocol is wired.
  const res = await fetchOk('POST', `${objectUrl(bucket, key)}?uploadId=${encodeURIComponent(uploadId)}`, {
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ parts }),
  })
  return res.json()
}

export async function abortMultipart(bucket: string, key: string, uploadId: string): Promise<void> {
  await fetchOk('DELETE', `${objectUrl(bucket, key)}?uploadId=${encodeURIComponent(uploadId)}`)
}
