// Thin fetch wrappers around the backend. Paths are relative, so they hit the
// same origin in production (the Rust binary serves both this SPA and the API)
// and are proxied to :8080 in dev (see vite.config.js).

async function asJson(res) {
    const body = await res.json().catch(() => ({}));
    if (!res.ok)
        throw new Error(body.error ?? `request failed (${res.status})`);
    return body;
}

/** POST /api/links — authenticated write path. */
export function createLink({ url, customSlug, apiKey }) {
    return fetch("/api/links", {
        method: "POST",
        headers: {
            "content-type": "application/json",
            ...(apiKey ? { authorization: `Bearer ${apiKey}` } : {}),
        },
        body: JSON.stringify({
            url,
            ...(customSlug ? { custom_slug: customSlug } : {}),
        }),
    }).then(asJson);
}

/** GET /api/debug/resolve/{slug} — public cache + Snowflake inspector. */
export function resolveDebug(slug) {
    return fetch(`/api/debug/resolve/${encodeURIComponent(slug)}`).then(asJson);
}
