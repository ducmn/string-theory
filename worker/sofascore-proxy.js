// Tiny Cloudflare Worker that proxies requests to Sofascore.
//
// Why: api.sofascore.com is Cloudflare-protected and 403s every cloud-egress
// IP (GitHub Actions, AWS, GCP) regardless of TLS fingerprint or headers.
// Cloudflare egress IPs aren't blocked (they ARE Cloudflare), so a Worker is
// the simplest, free-tier-friendly bypass.
//
// Deploy:
//   npx wrangler deploy
// (or paste this file into the Cloudflare dashboard's "Quick Edit" UI).
//
// Then set the resulting *.workers.dev URL as the GitHub Actions secret
// `SOFASCORE_PROXY_BASE`, including the trailing `/api/v1` segment.

const UPSTREAM = "https://api.sofascore.com";

export default {
  async fetch(request) {
    const incoming = new URL(request.url);
    const target = new URL(UPSTREAM + incoming.pathname + incoming.search);

    // Trim incoming headers to a clean browser-y set. Don't forward
    // Worker-injected headers like cf-* or x-real-ip.
    const fwdHeaders = new Headers({
      "Accept": "application/json, text/plain, */*",
      "Accept-Language": "en-GB,en-US;q=0.9",
      "Origin": "https://www.sofascore.com",
      "Referer": "https://www.sofascore.com/",
      "User-Agent":
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " +
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    });

    const upstream = await fetch(target.toString(), {
      method: request.method,
      headers: fwdHeaders,
      // Sofascore endpoints we need are GET-only; ignore body.
      cf: { cacheTtl: 60, cacheEverything: true },
    });

    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") || "application/json",
        "Cache-Control": "public, max-age=60",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
