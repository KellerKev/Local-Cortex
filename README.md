<p align="center">
  <img src="assets/logo.svg" alt="Local-Cortex" width="560"/>
</p>

<h1 align="center">Local-Cortex</h1>
<p align="center">
  <em>Snowflake Cortex Code CLI, fully local. Pluggable backends. Optional hybrid mode for real SQL.</em>
</p>

<p align="center">
  <img src="assets/demo.gif" alt="Local-Cortex in action" width="820"/>
</p>

<p align="center">
  <img src="assets/llama-cpp-1.jpg" alt="Cortex Code driven by llama.cpp via Local-Cortex (1/2)" width="49%"/>
  <img src="assets/llama-cpp-2.jpg" alt="Cortex Code driven by llama.cpp via Local-Cortex (2/2)" width="49%"/>
</p>

Run Snowflake **Cortex Code CLI** against any LLM backend — local **Ollama**,
**OpenAI** (or any OpenAI-compat provider — xAI/Groq/OpenRouter/llama.cpp/
LMStudio), or **Anthropic** — without ever touching `snowflakecomputing.com`
for inference. SQL tools can be stubbed (fully-local) or routed at a real
Snowflake account (hybrid).

## How it works

Two undocumented Cortex hooks do all the heavy lifting:

1. `CORTEX_AGENT_USE_LOCAL_ORCHESTRATOR=1` forces Cortex's agent loop to
   `http://localhost:2031/v1/agent-run`. We implement that endpoint and
   stream the reply back as the Anthropic-style SSE events Cortex parses.
2. A fake `ollama` connection in `~/.snowflake/config.toml` points Cortex's
   Node SDK at `https://localhost:2443`. The proxy serves a minimal stub for
   `/session/v1/login-request` and friends so the SDK thinks auth succeeded.

With those in place, Cortex starts up, authenticates against the proxy, and
routes every agent turn to whichever backend you've configured.

## Quick start

You need three things on `$PATH`:

1. **Cortex Code CLI** — install per Snowflake docs (`https://ai.snowflake.com/static/cc-scripts/install.sh`).
2. **Ollama** — `https://ollama.com/download` and `ollama pull <some-model>`.
3. **Python 3.11+** and **`openssl`** (both already on macOS/most Linux).

Pick one of two install paths.

### Path A — with pixi (recommended; reproducible env)

```bash
git clone https://github.com/KellerKev/Local-Cortex.git
cd Local-Cortex
pixi install                                         # ~40 MB python env
pixi run gen-cert                                    # self-signed cert for :2443
cp configs/ollama.toml cortex_ollama.toml            # any configs/*.toml works

# add the proxy stub connection to ~/.snowflake/config.toml
cat >> ~/.snowflake/config.toml <<'EOF'

[connections.ollama]
account = "ollamaproxy"
host = "localhost"
port = 2443
user = "ollama"
password = "dummy-pat-token"
authenticator = "PROGRAMMATIC_ACCESS_TOKEN"
role = "PUBLIC"
EOF

pixi run tui                                         # done — TUI boots
```

### Path B — without pixi (plain venv)

```bash
git clone https://github.com/KellerKev/Local-Cortex.git
cd Local-Cortex

python3 -m venv .venv && source .venv/bin/activate
pip install fastapi uvicorn httpx pydantic

# self-signed cert for the HTTPS listener (one-shot)
mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
  -keyout certs/localhost.key -out certs/localhost.crt \
  -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1'

cp configs/ollama.toml cortex_ollama.toml

# same connection block as Path A, see above
cat >> ~/.snowflake/config.toml <<'EOF'

[connections.ollama]
account = "ollamaproxy"
host = "localhost"
port = 2443
user = "ollama"
password = "dummy-pat-token"
authenticator = "PROGRAMMATIC_ACCESS_TOKEN"
role = "PUBLIC"
EOF

# shell 1: run the proxy
python -m proxy

# shell 2: drive Cortex
export CORTEX_AGENT_USE_LOCAL_ORCHESTRATOR=1
export NODE_TLS_REJECT_UNAUTHORIZED=0
cortex -c ollama --no-auto-update
```

That's it. Cortex TUI now talks to your local Ollama for every turn.

## What's verified

End-to-end tested as of the latest commit:

| Claim | Verified how |
|---|---|
| Ollama backend, text + tool calls + multi-turn | live Cortex turns; tool round-trip with `read` and `bash`; SQL `sql_execute` against real Snowflake |
| OpenAI backend against Ollama's `/v1` | live Cortex turn through `openai_compat` adapter (free, no API key) |
| OpenAI backend against any OpenAI-compat endpoint (xAI / Groq / OpenRouter / vLLM / llama.cpp / LMStudio) | live test against `llama-server` at `:8092` |
| Anthropic backend — text streaming | live via LiteLLM → Ollama; SSE grammar matches `api.anthropic.com` |
| Anthropic backend — tool calling | unit-tested with mock SSE; real Anthropic should work; LiteLLM→Ollama tool path crashes the runner upstream |
| Hot-swap model via `POST /model` | observed next-turn change in proxy log |
| Hot-swap backend via `POST /backend` | observed via `/healthz` reflecting the change |
| Hybrid mode SQL routing | live `CURRENT_ACCOUNT()` against two real accounts (AWS Frankfurt, Azure West Europe) |
| `agent_connection` TUI label + auto-add of connection block | live test with `agent_connection = "llama-cpp"` |
| Update probe | 18 anchors pass on Cortex 1.0.73; fails loudly if any required string disappears |
| Non-pixi install path | live test in a clean Python 3.13 venv (only fastapi/uvicorn/httpx/pydantic) |

## Features

- **Pluggable backends** — `ollama` (local, default), `openai`
  (covers OpenAI itself plus xAI/Groq/OpenRouter/Together/vLLM/llama.cpp via
  their OpenAI-compat endpoints), and `anthropic` (Messages API). Hot-swap
  any of them without restarting the proxy via `POST /backend`.
- **Tool calling round-trip** — bare built-ins (`read`, `write`, `edit`,
  `bash`, `bash_output`, `kill_shell`, `grep`, `glob`, `web_fetch`,
  `web_search`, `sql_execute`, `notebook_actions`, …) plus every
  client-MCP tool Cortex ships with its own schema (`task_*`, `team_*`,
  `cron_*`, `skill`). Translator handles structured `tool_calls` *and*
  qwen-style `<function=…>` text fallback; the result round-trip maps
  Cortex's nested `tool_use.*` / `tool_result.*` shapes to OpenAI /
  Anthropic message shapes. (Built-ins exercised live; MCP-style tools
  follow the same translation path so they should round-trip the same
  way — open an issue if a specific one misbehaves.)
- **Hybrid mode** — set `sql_connection` and the `sql_execute` tool routes to
  a real Snowflake account (via Cortex's native `sqlConnectionName`), with a
  proxy-side safety net that pins `connection:` on every Snowflake-family
  tool call.
- **Thinking-block stripping** — `<think>…</think>` from Qwen/DeepSeek
  reasoning models is hidden from the user-visible output.
- **Update-robustness probe** — scans the installed Cortex binary for the
  protocol anchors the translator depends on. Runs at server startup and
  aborts if a Cortex update has renamed something we rely on.
- **Single TOML config** — `cortex_ollama.toml` holds backend creds, ports,
  and Snowflake routing in one place. Env vars still override per-launch.

## First-time setup

```bash
pixi run gen-cert          # self-signed cert for localhost HTTPS listener
pixi install               # installs the python env (~40 MB)

# Pick a scenario and copy its config in. Each file under configs/ is a
# self-contained example you can use as-is or as a starting point.
cp configs/ollama.toml cortex_ollama.toml         # local Ollama (default)
# or
cp configs/ollama-hybrid.toml cortex_ollama.toml  # Ollama + real Snowflake SQL
# or
cp configs/openai.toml cortex_ollama.toml         # OpenAI / xAI / Groq / …
# or
cp configs/anthropic.toml cortex_ollama.toml      # real Anthropic
# or
cp configs/multi.toml cortex_ollama.toml          # all backends; hot-swap via REST
```

Available samples:

| File | Backend | SQL | Notes |
|---|---|---|---|
| `configs/ollama.toml`               | Ollama (`/api/chat`) | stubbed       | default; fully-local |
| `configs/ollama-hybrid.toml`        | Ollama               | real Snowflake | edit `sql_connection` |
| `configs/openai-via-ollama.toml`    | OpenAI-compat → Ollama | stubbed     | free test for openai backend |
| `configs/openai.toml`               | OpenAI / xAI / Groq / etc. | stubbed | paste an api_key |
| `configs/anthropic.toml`            | Anthropic Messages   | stubbed       | paste an api_key |
| `configs/anthropic-via-litellm.toml`| Anthropic → LiteLLM → Ollama | stubbed | needs LiteLLM proxy; text path only |
| `configs/multi.toml`                | all three configured | stubbed       | switch via `POST /backend` |

The proxy looks for its config at, in order:

1. `$CORTEX_OLLAMA_CONFIG` (explicit path)
2. `./cortex_ollama.toml` (project-local)
3. `~/.config/cortex-ollama/config.toml` (per-user)

If none exists, built-in defaults kick in (Ollama on `localhost:11434`,
fully-local SQL).

Then add the `ollama` connection to `~/.snowflake/config.toml` (this is where
the Snowflake Node SDK reads connection blocks from — that location is fixed
by Cortex itself):

```toml
[connections.ollama]
account = "ollamaproxy"
host = "localhost"
port = 2443
user = "ollama"
password = "dummy-pat-token"
authenticator = "PROGRAMMATIC_ACCESS_TOKEN"
role = "PUBLIC"
```

## Run

In one shell:
```bash
pixi run serve             # HTTP :2031 (agent:run) + HTTPS :2443 (Snowflake)
```

In another, either the raw path:
```bash
export CORTEX_AGENT_USE_LOCAL_ORCHESTRATOR=1
export NODE_TLS_REJECT_UNAUTHORIZED=0
cortex -c ollama -p "explain this repo"
```

…or use the bundled wrapper that sets all the env vars for you:
```bash
pixi run cortex -- -p "explain this repo"
pixi run cortex --                 # interactive session
```

### Choosing a backend

Edit `cortex_ollama.toml`:

```toml
backend = "ollama"   # or "openai", "anthropic"

[backends.ollama]
base_url = "http://127.0.0.1:11434"
model    = "qwen3.6:35b-a3b"

[backends.openai]
base_url = "https://api.openai.com/v1"   # or any OpenAI-compatible endpoint
api_key  = "sk-..."                       # or set OPENAI_API_KEY in env
model    = "gpt-4o"

[backends.anthropic]
base_url = "https://api.anthropic.com/v1"
api_key  = "sk-ant-..."                   # or set ANTHROPIC_API_KEY in env
model    = "claude-sonnet-4-5"
```

Switch at runtime without restarting the proxy:

```bash
curl -X POST http://127.0.0.1:2031/backend \
     -H 'Content-Type: application/json' \
     -d '{"backend": "anthropic"}'
# next agent turn goes to Anthropic
```

### Switching models within a backend

```bash
# Cold start with a specific model
OLLAMA_MODEL=devstral-small-2:latest pixi run tui

# Permanent: edit cortex_ollama.toml and restart, or:
export OLLAMA_MODEL=qwen3.6:35b-a3b

# What's installed locally?
pixi run cortex -- --list-models     # ollama backend only

# Mid-session swap (works for any backend)
curl -X POST http://127.0.0.1:2031/model \
     -H 'Content-Type: application/json' \
     -d '{"model": "qwen3-coder:30b"}'
```

Inside the TUI, the bundled `/local-models` slash command prints the swap
recipe so you don't have to leave the session.

### Testing the OpenAI / Anthropic backends without paid keys

Ollama exposes an **OpenAI-compatible** `/v1` endpoint, so you can exercise
the `openai` backend end-to-end against your local Ollama for free:

```toml
[backends.openai]
base_url = "http://127.0.0.1:11434/v1"
api_key  = "ollama"           # any non-empty string
model    = "qwen3.6:35b-a3b"   # whatever you have in `ollama list`
```

Switch to it (`backend = "openai"`), restart the proxy, and Cortex still
works — just routed through `/v1/chat/completions` instead of `/api/chat`.

For the **Anthropic** backend, Ollama does *not* implement `/v1/messages`.
Run [LiteLLM](https://docs.litellm.ai/docs/proxy/quick_start) as a translator
in front of Ollama:

```bash
pipx install 'litellm[proxy]'
litellm --model ollama_chat/qwen3.6:35b-a3b --port 14000 --drop_params
```

Then point the proxy at LiteLLM (note `/v1` in the URL — LiteLLM exposes the
Anthropic endpoint at `/v1/messages`):

```toml
[backends.anthropic]
base_url = "http://127.0.0.1:14000/v1"
api_key  = "anything"
model    = "ollama_chat/qwen3.6:35b-a3b"
```

What works in this loop (verified):

- **Text streaming end-to-end** — Cortex sees `content_block_start` then
  `response.text.delta` tokens streamed back through the Anthropic adapter,
  LiteLLM, and Ollama. The full proxy ↔ Anthropic SSE grammar is exercised.

What doesn't (upstream limitation, not in our code):

- **Tool-calling** — Cortex sends ~37 tools per turn. LiteLLM translates the
  Anthropic-shaped tool defs into Ollama's chat template, and qwen-family
  models tend to crash the llama runner under that combined payload
  (`llama runner terminated, exit status 2`). The Anthropic adapter itself
  is correct; against real `api.anthropic.com` with a Claude model this path
  works fine.

### REST surface

```bash
GET  /healthz   # backend, base_url, model, timestamp
GET  /models    # backend's available model list
GET  /model     # current model + configured default
POST /model     # body: {"model": "..."}    — hot-swap model on current backend
GET  /backend   # current backend + configured ones
POST /backend   # body: {"backend": "anthropic", "model": "..."}  — hot-swap backend
```

The proxy validates against `ollama list` before accepting; an unknown name
returns 400 with the available models so a misspelling never silently fails.
On proxy restart the active model resets to `OLLAMA_MODEL` (or the built-in
default). To make a change permanent, set `OLLAMA_MODEL` in the shell that
launches `pixi run serve`.

## Hybrid mode: local AI + real Snowflake for SQL

Cortex internally separates its **agent connection** (inference) from its
**SQL connection** (database queries) — each tracked as an independent value
and exposed via env vars the CLI already honors. cortex_ollama uses this to
run a split configuration: reasoning, tool-call planning, and code editing
all happen against local Ollama; `snowflake_sql_execute` and its siblings
route to a real Snowflake account.

1. Create a PAT in your Snowflake account and add it as a second connection:

   ```toml
   # ~/.snowflake/config.toml

   [connections.ollama]           # already configured — used for agent/auth
   account = "ollamaproxy"
   host = "localhost"
   port = 2443
   authenticator = "PROGRAMMATIC_ACCESS_TOKEN"
   password = "dummy-pat-token"
   user = "ollama"

   [connections.sf-real]          # add this — real account, real PAT
   account = "YOUR_ACCOUNT_ID"
   user = "your-username"
   password = "<paste your PAT here>"
   role = "ACCOUNTADMIN"          # or whatever your PAT permits
   warehouse = "COMPUTE_WH"
   ```

   *Note*: leave the `authenticator` line **off**. Some Snowflake accounts
   reject PATs when authenticator is set explicitly to
   `PROGRAMMATIC_ACCESS_TOKEN`, but accept the same token as a regular
   password (auto-detected). The fully-local `[connections.ollama]` above
   keeps the explicit authenticator because our HTTPS stub relies on the
   declared PAT path to skip browser auth.

2. Start the proxy with the SQL connection name available:

   ```bash
   CORTEX_SQL_CONNECTION=sf-real pixi run serve
   ```

3. Run Cortex via the wrapper — it picks up `CORTEX_SQL_CONNECTION` and
   plumbs it through Cortex's native split-connection config:

   ```bash
   CORTEX_SQL_CONNECTION=sf-real pixi run cortex -- \
     -p "show the 5 most-queried tables in my account last week"
   ```

**How routing works end-to-end:**

- Cortex sees `CORTEX_SQL_CONNECTION=sf-real` and treats `sf-real` as the
  default `sqlConnectionName`. SQL-family tools (`snowflake_sql_execute`,
  `snowflake_object_search`, `snowflake_product_docs`, `semantic_view_search`)
  use that connection when the model doesn't pass `connection:` explicitly.
- Cortex also injects a "connection change" system reminder so the model
  knows which account it's about to query — no prompt engineering needed.
- **Safety net**: when the proxy sees a Snowflake tool_use emitted by Ollama
  without a `connection:` field — or with a `connection:` that incorrectly
  names the agent connection (the proxy stub) — it rewrites the argument to
  `sf-real` before forwarding to Cortex. Guards against model drift on
  long conversations.

### Env vars honored by the proxy

| var                      | default                  | meaning                                    |
|--------------------------|--------------------------|--------------------------------------------|
| `OLLAMA_BASE_URL`        | `http://127.0.0.1:11434` | Ollama server                              |
| `OLLAMA_MODEL`           | `qwen3.6:35b-a3b`        | model name passed to Ollama                |
| `CORTEX_AGENT_CONNECTION`| `ollama`                 | connection used for the agent loop (stay on the proxy) |
| `CORTEX_SQL_CONNECTION`  | unset                    | connection used for SQL tools (set for hybrid mode)   |
| `CORTEX_PROXY_HTTP_PORT` | `2031`                   | plaintext listener (agent:run) port        |
| `CORTEX_PROXY_HTTPS_PORT`| `2443`                   | TLS listener (Snowflake auth) port         |
| `CORTEX_OLLAMA_DEBUG`    | unset                    | dump every request payload to `/tmp`       |
| `CORTEX_SKIP_PROBE`      | unset                    | skip the startup anchor-probe              |


## Update robustness

Cortex Code is shipped as a Bun-packaged Mach-O. Minified identifiers (e.g.
`i3L`, `gz`, `aE$`) can change on any release, but the wire-contract strings
the proxy depends on — the env-var name, the `/v1/agent-run` URL, the SSE
event names, `client_side_execute`, `PROGRAMMATIC_ACCESS_TOKEN`, etc. — are
part of Snowflake's cross-version orchestrator protocol and won't move on a
whim.

The probe checks 15 such anchors.

```bash
pixi run probe                # human-readable
pixi run probe --json         # machine-readable, exits 1 on any miss
```

A fresh Cortex install triggers the probe automatically on `pixi run serve`;
a drift is caught before Cortex ever sees a broken response.

If a future Cortex release **does** rename a required anchor:

1. The probe fails on startup with `FAIL: N required anchor(s) missing.`
2. Run `pixi run capture` to log raw request bodies from the new Cortex.
3. Compare the captured payload against `captures/20260422-*` to spot the
   schema delta and update [proxy/server.py](proxy/server.py) / probe anchors.

## Files

- [proxy/server.py](proxy/server.py) — agent:run translator + SSE emitter
- [proxy/snowflake_stubs.py](proxy/snowflake_stubs.py) — fake Snowflake auth endpoints
- [proxy/toolspecs.py](proxy/toolspecs.py) — schemas for Cortex's built-in tools
- [proxy/probe.py](proxy/probe.py) — anchor-string verifier against the cortex binary
- [proxy/__main__.py](proxy/__main__.py) — dual HTTP + HTTPS entrypoint
- [proxy/capture.py](proxy/capture.py) — raw request logger (for future reverse-engineering)

Reverse-engineered against Cortex Code `1.0.48+043705`.
