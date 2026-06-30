import { useState } from "react";
import { createLink, resolveDebug } from "./api.js";

export default function App() {
  return (
    <main className="wrap">
      <header>
        <h1>🔗 URL Shortener</h1>
        <p className="muted">
          Vite + React dashboard, embedded in the Rust binary via{" "}
          <code>rust-embed</code> and served at <code>/</code>.
        </p>
      </header>
      <CreatePanel />
      <ResolvePanel />
    </main>
  );
}

function CreatePanel() {
  const [url, setUrl] = useState("https://example.com/some/long/path");
  const [customSlug, setCustomSlug] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await createLink({ url, customSlug, apiKey }));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card">
      <h2>Create a short link</h2>
      <form onSubmit={onSubmit}>
        <label>
          Long URL (https only)
          <input value={url} onChange={(e) => setUrl(e.target.value)} required />
        </label>
        <label>
          Custom slug (optional)
          <input
            value={customSlug}
            onChange={(e) => setCustomSlug(e.target.value)}
            placeholder="auto-generated if blank"
          />
        </label>
        <label>
          API key (Bearer)
          <input
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder="required for POST /api/links"
          />
        </label>
        <button disabled={busy}>{busy ? "Creating…" : "Shorten"}</button>
      </form>
      {error && <p className="error">⚠ {error}</p>}
      {result && (
        <div className="result">
          <a href={result.short_url}>{result.short_url}</a>
          <pre>{JSON.stringify(result, null, 2)}</pre>
        </div>
      )}
    </section>
  );
}

function ResolvePanel() {
  const [slug, setSlug] = useState("");
  const [info, setInfo] = useState(null);
  const [error, setError] = useState(null);

  async function onResolve(e) {
    e.preventDefault();
    setError(null);
    setInfo(null);
    try {
      setInfo(await resolveDebug(slug));
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <section className="card">
      <h2>Resolve — cache + Snowflake inspector</h2>
      <form onSubmit={onResolve}>
        <label>
          Slug
          <input
            value={slug}
            onChange={(e) => setSlug(e.target.value)}
            placeholder="the code from a short_url"
            required
          />
        </label>
        <button>Resolve</button>
      </form>
      {error && <p className="error">⚠ {error}</p>}
      {info && (
        <div className="result">
          <p>
            <strong>{info.found ? "found" : "not found"}</strong> · cache:{" "}
            <code>{info.cache}</code> · {info.served_from} ·{" "}
            {info.latency_ms.toFixed(2)} ms
          </p>
          <pre>{JSON.stringify(info, null, 2)}</pre>
        </div>
      )}
    </section>
  );
}
