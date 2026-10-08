from pathlib import Path

root = Path("upstream")
web = root / "apps/photocraft-web"
cargo = web / "Cargo.toml"
src = web / "src" / "web.rs"
html = web / "index.html"

def replace_once(path, old, new):
    text = path.read_text()
    if old not in text:
        raise SystemExit("patch anchor not found in " + str(path))
    path.write_text(text.replace(old, new, 1))

c = cargo.read_text()
if "serde_json = { workspace = true }" not in c:
    c = c.replace("log = { workspace = true }\n", "log = { workspace = true }\nserde_json = { workspace = true }\n", 1)
cargo.write_text(c)

s = src.read_text()
s = s.replace(
    "use std::sync::{Arc, Mutex};\n",
    "use std::cell::RefCell;\nuse std::sync::{Arc, Mutex};\nuse std::sync::atomic::{AtomicU64, Ordering};\n\nuse serde_json::{Value, json};\nuse wasm_bindgen::prelude::*;\n",
    1,
)

marker = 'const CANVAS_ID: &str = "photocraft_canvas";\n'
bridge = r'''
type AgentQueue = Arc<Mutex<Vec<(String, String, String)>>>;
type AgentResults = Arc<Mutex<Vec<String>>>;

thread_local! {
    static AGENT_QUEUE: RefCell<Option<AgentQueue>> = const { RefCell::new(None) };
    static AGENT_RESULTS: RefCell<Option<AgentResults>> = const { RefCell::new(None) };
}

static NEXT_AGENT_ID: AtomicU64 = AtomicU64::new(1);

#[wasm_bindgen]
pub fn agent_run(command_id: &str, params_json: &str) -> String {
    let id = format!("a{}", NEXT_AGENT_ID.fetch_add(1, Ordering::Relaxed));
    let params = match serde_json::from_str::<Value>(params_json) {
        Ok(v) => v,
        Err(e) => {
            let reply = json!({"id": id, "ok": false, "error": format!("invalid params JSON: {}", e)});
            AGENT_RESULTS.with(|r| {
                if let Some(results) = r.borrow().as_ref() {
                    if let Ok(mut out) = results.lock() { out.push(reply.to_string()); }
                }
            });
            return id;
        }
    };
    AGENT_QUEUE.with(|q| {
        if let Some(queue) = q.borrow().as_ref() {
            if let Ok(mut pending) = queue.lock() {
                pending.push((id.clone(), command_id.to_string(), params.to_string()));
            }
        }
    });
    id
}

#[wasm_bindgen]
pub fn agent_poll() -> String {
    AGENT_RESULTS.with(|r| {
        let binding = r.borrow();
        let Some(results) = binding.as_ref() else { return "[]".to_string() };
        let Ok(mut out) = results.lock() else { return "[]".to_string() };
        let values: Vec<Value> = out.drain(..).filter_map(|s| serde_json::from_str(&s).ok()).collect();
        serde_json::to_string(&values).unwrap_or_else(|_| "[]".to_string())
    })
}

#[wasm_bindgen]
pub fn agent_commands() -> String {
    let values: Vec<Value> = photocraft_engine::command_specs()
        .iter()
        .map(|c| json!({
            "id": c.id,
            "label": c.label,
            "menu": c.menu,
            "shortcut": c.shortcut,
            "params": c.params
        }))
        .collect();
    serde_json::to_string(&values).unwrap_or_else(|_| "[]".to_string())
}

'''
if marker not in s:
    raise SystemExit("CANVAS_ID marker missing")
s = s.replace(marker, marker + bridge, 1)

old = 'let inbox: Inbox = Arc::default();\n                    let mut app = PhotocraftApp::new(Session::new(), services(inbox.clone(), cc.egui_ctx.clone()));'
new = '''let inbox: Inbox = Arc::default();
                    let agent_queue: AgentQueue = Arc::default();
                    let agent_results: AgentResults = Arc::default();
                    AGENT_QUEUE.with(|q| *q.borrow_mut() = Some(agent_queue.clone()));
                    AGENT_RESULTS.with(|r| *r.borrow_mut() = Some(agent_results.clone()));
                    let mut app = PhotocraftApp::new(Session::new(), services(inbox.clone(), cc.egui_ctx.clone()));'''
if old not in s:
    raise SystemExit("app creation anchor missing")
s = s.replace(old, new, 1)
s = s.replace("Ok(Box::new(WebShell { app, inbox }))", "Ok(Box::new(WebShell { app, inbox, agent_queue, agent_results }))", 1)

s = s.replace(
'''struct WebShell {
    app: PhotocraftApp,
    inbox: Inbox,
}''',
'''struct WebShell {
    app: PhotocraftApp,
    inbox: Inbox,
    agent_queue: AgentQueue,
    agent_results: AgentResults,
}''',
1)

old_logic = '''    fn logic(&mut self, ctx: &egui::Context, frame: &mut eframe::Frame) {
        let dropped = ctx.input_mut(|i| std::mem::take(&mut i.raw.dropped_files));'''
new_logic = '''    fn logic(&mut self, ctx: &egui::Context, frame: &mut eframe::Frame) {
        let pending = {
            let mut q = self.agent_queue.lock().unwrap_or_else(|e| e.into_inner());
            std::mem::take(&mut *q)
        };
        for (request_id, command_id, params_json) in pending {
            let result = match serde_json::from_str::<Value>(&params_json) {
                Ok(params) => self.app.session.execute(&command_id, params).map_err(|e| e.to_string()),
                Err(e) => Err(format!("invalid params JSON: {}", e)),
            };
            let reply = match result {
                Ok(value) => json!({"id": request_id, "ok": true, "command": command_id, "result": value}),
                Err(error) => json!({"id": request_id, "ok": false, "command": command_id, "error": error}),
            };
            if let Ok(mut out) = self.agent_results.lock() { out.push(reply.to_string()); }
        }

        let dropped = ctx.input_mut(|i| std::mem::take(&mut i.raw.dropped_files));'''
if old_logic not in s:
    raise SystemExit("logic anchor missing")
s = s.replace(old_logic, new_logic, 1)
src.write_text(s)

h = html.read_text()
injected = r'''
    <style>
      #agent_panel {
        position: fixed; z-index: 10000; right: 14px; bottom: 14px;
        width: min(390px, calc(100vw - 28px)); height: min(520px, calc(100vh - 28px));
        display: flex; flex-direction: column; overflow: hidden;
        background: rgba(22,22,24,.96); color: #eee; border: 1px solid #45454b;
        border-radius: 14px; box-shadow: 0 12px 40px rgba(0,0,0,.45);
        font: 13px system-ui, sans-serif; backdrop-filter: blur(12px);
      }
      #agent_header { padding: 12px 14px; border-bottom: 1px solid #3a3a40; font-weight: 700; }
      #agent_log { flex: 1; overflow: auto; padding: 12px; display: flex; flex-direction: column; gap: 9px; }
      .agent_msg { padding: 9px 11px; border-radius: 10px; white-space: pre-wrap; line-height: 1.45; }
      .agent_user { background: #303038; align-self: flex-end; max-width: 90%; }
      .agent_ai { background: #242429; border: 1px solid #3b3b42; }
      #agent_form { display: flex; gap: 7px; padding: 9px; border-top: 1px solid #3a3a40; }
      #agent_input { flex: 1; resize: none; min-height: 42px; max-height: 110px; border-radius: 9px;
        border: 1px solid #4a4a52; background: #17171b; color: #fff; padding: 9px; outline: none; }
      #agent_send { border: 0; border-radius: 9px; padding: 0 13px; background: #eee; color: #111; font-weight: 700; }
      #agent_send:disabled { opacity: .5; }
      @media (max-width: 600px) {
        #agent_panel { right: 7px; bottom: 7px; width: calc(100vw - 14px); height: min(58vh, 480px); }
      }
    </style>
    <section id="agent_panel" aria-label="PhotoCraft AI Agent">
      <div id="agent_header">PhotoCraft AI Agent</div>
      <div id="agent_log"><div class="agent_msg agent_ai">دستور خود را بنویسید. Agent فرمان مناسب PhotoCraft را انتخاب و اجرا می‌کند.</div></div>
      <form id="agent_form">
        <textarea id="agent_input" placeholder="مثلاً: تصویر را مربع کن، روشنایی را کمی زیاد کن و PNG خروجی بگیر"></textarea>
        <button id="agent_send" type="submit">اجرا</button>
      </form>
    </section>
    <script>
      (() => {
        const $ = (id) => document.getElementById(id);
        const log = $("agent_log"), input = $("agent_input"), send = $("agent_send");
        const add = (text, cls) => {
          const d = document.createElement("div"); d.className = "agent_msg " + cls; d.textContent = text;
          log.appendChild(d); log.scrollTop = log.scrollHeight;
        };
        const waitForWasm = () => new Promise(resolve => {
          if (window.wasmBindings && typeof window.wasmBindings.agent_run === "function") return resolve();
          window.addEventListener("TrunkApplicationStarted", () => resolve(), {once:true});
        });
        const poll = async () => {
          if (!window.wasmBindings?.agent_poll) return;
          const items = JSON.parse(window.wasmBindings.agent_poll() || "[]");
          for (const r of items) {
            if (r.ok) add("✓ " + r.command, "agent_ai");
            else add("✗ " + r.command + ": " + r.error, "agent_ai");
          }
        };
        setInterval(poll, 120);
        $("agent_form").addEventListener("submit", async (e) => {
          e.preventDefault();
          const message = input.value.trim(); if (!message) return;
          input.value = ""; add(message, "agent_user"); send.disabled = true;
          try {
            await waitForWasm();
            const commands = JSON.parse(window.wasmBindings.agent_commands());
            const r = await fetch("/api/agent", {
              method: "POST", headers: {"content-type":"application/json"},
              body: JSON.stringify({message, commands})
            });
            const data = await r.json();
            if (!r.ok) throw new Error(data.error || "Agent request failed");
            if (data.message) add(data.message, "agent_ai");
            for (const a of data.actions || []) {
              window.wasmBindings.agent_run(a.id, JSON.stringify(a.params || {}));
            }
          } catch (err) {
            add("خطا: " + (err?.message || err), "agent_ai");
          } finally { send.disabled = false; }
        });
      })();
    </script>
'''
if "</body>" not in h:
    raise SystemExit("body anchor missing")
html.write_text(h.replace("</body>", injected + "\n</body>", 1))
