const MODEL = "@cf/meta/llama-3.1-8b-instruct";

const ACTION_SCHEMA = {
  type: "object",
  properties: {
    message: { type: "string" },
    actions: {
      type: "array",
      items: {
        type: "object",
        properties: {
          id: { type: "string" },
          params: { type: "object" }
        },
        required: ["id", "params"],
        additionalProperties: false
      }
    }
  },
  required: ["message", "actions"],
  additionalProperties: false
};

function json(data, status = 200) {
  return Response.json(data, { status, headers: { "Cache-Control": "no-store" } });
}

function extractResponse(result) {
  if (typeof result === "string") return result;
  if (result && typeof result.response === "string") return result.response;
  if (result?.choices?.[0]?.message?.content) return result.choices[0].message.content;
  return "";
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/api/agent") {
      if (request.method !== "POST") return json({ error: "POST required" }, 405);
      let body;
      try { body = await request.json(); } catch { return json({ error: "Invalid JSON" }, 400); }

      const message = typeof body.message === "string" ? body.message.trim() : "";
      const commands = Array.isArray(body.commands) ? body.commands : [];
      if (!message) return json({ error: "message is required" }, 400);
      if (!commands.length) return json({ error: "PhotoCraft command registry is unavailable" }, 400);
      if (commands.length > 700) return json({ error: "command catalog is unexpectedly large" }, 400);

      const catalog = commands.map(c => ({
        id: c.id, label: c.label, menu: c.menu, shortcut: c.shortcut, params: c.params
      }));
      const allowed = new Set(catalog.map(c => c.id));

      const system = [
        "You are the PhotoCraft image-editing agent.",
        "The browser runs the real PhotoCraft Rust engine. You do not edit pixels yourself.",
        "Translate the user's natural-language request into one or more exact PhotoCraft engine commands.",
        "Use only command IDs from the supplied registry. Never invent an ID.",
        "Use each command's documented params syntax.",
        "Prefer the smallest reliable sequence.",
        "If the requested operation is not represented by the registry, return an empty actions array and explain the limitation.",
        "Do not use shell commands, JavaScript, URLs, arbitrary file paths, or tools outside the registry.",
        "Actions are executed automatically in the user's browser.",
        "Respond in Persian when the user writes Persian.",
        "",
        "PHOTOCRAFT COMMAND REGISTRY:",
        JSON.stringify(catalog)
      ].join("\n");

      let result;
      try {
        result = await env.AI.run(MODEL, {
          messages: [
            { role: "system", content: system },
            { role: "user", content: message }
          ],
          temperature: 0.1,
          max_tokens: 1800,
          response_format: { type: "json_schema", json_schema: ACTION_SCHEMA }
        });
      } catch (e) {
        return json({ error: "AI inference failed", detail: String(e) }, 502);
      }

      let parsed = result?.response;
      if (typeof parsed === "string") {
        try { parsed = JSON.parse(parsed); } catch { return json({ error: "AI returned invalid structured output" }, 502); }
      }
      if (!parsed || typeof parsed !== "object") {
        try { parsed = JSON.parse(extractResponse(result)); } catch { return json({ error: "AI returned no valid plan" }, 502); }
      }

      const actions = Array.isArray(parsed.actions) ? parsed.actions : [];
      if (actions.length > 30) return json({ error: "Agent produced too many actions" }, 422);

      const safeActions = [];
      for (const action of actions) {
        if (!action || typeof action.id !== "string" || !allowed.has(action.id)) {
          return json({ error: "Agent selected a command outside PhotoCraft's registry" }, 422);
        }
        const params = action.params && typeof action.params === "object" && !Array.isArray(action.params) ? action.params : {};
        safeActions.push({ id: action.id, params });
      }

      return json({
        message: typeof parsed.message === "string" ? parsed.message : "",
        actions: safeActions
      });
    }
    return env.ASSETS.fetch(request);
  }
};
