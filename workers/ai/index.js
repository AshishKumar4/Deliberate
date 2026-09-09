// Local workerd handles HTTP; Wrangler authenticates the remote AI binding.
// Forward the provider response unchanged. ReasonProxy validates its protocol.
const loopback = new Set(["localhost", "127.0.0.1", "[::1]"]);
const failure = (status, type, message) =>
  Response.json({ error: { type, message } }, { status });

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!loopback.has(url.hostname) || request.headers.has("x-forwarded-for")) {
      return failure(403, "bridge_forbidden", "Localhost access only.");
    }
    if (url.pathname === "/diag/models" && request.method === "GET") {
      try {
        return Response.json(await env.AI.models(
          url.searchParams.has("search") ? { search: url.searchParams.get("search") } : {},
        ));
      } catch {
        return failure(502, "bridge_binding_error", "Remote AI catalogue request failed.");
      }
    }
    if (url.pathname !== "/v1/chat/completions") {
      return failure(404, "invalid_request_error", "Unknown endpoint.");
    }
    if (request.method !== "POST") {
      return failure(405, "invalid_request_error", "POST required.");
    }
    if (!request.headers.get("content-type")?.toLowerCase().startsWith("application/json")) {
      return failure(415, "invalid_request_error", "application/json required.");
    }
    let body;
    try {
      body = await request.json();
    } catch {
      return failure(400, "invalid_request_error", "Invalid JSON.");
    }
    if (!body || typeof body !== "object" || Array.isArray(body)
        || typeof body.model !== "string" || !body.model.trim()) {
      return failure(400, "invalid_request_error", "A model identifier is required.");
    }
    try {
      return await env.AI.run(body.model, body, { returnRawResponse: true });
    } catch {
      // Never copy SDK exception text: it may contain authorization material.
      return failure(502, "bridge_binding_error", "Remote AI binding request failed.");
    }
  },
};
