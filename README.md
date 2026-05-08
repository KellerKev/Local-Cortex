# cortex_ollama

Run Snowflake **Cortex Code CLI** entirely against a local **Ollama** server —
no Snowflake account, no browser auth, no network calls to `snowflakecomputing.com`.

## How it works

Two undocumented Cortex hooks do all the heavy lifting:

1. `CORTEX_AGENT_USE_LOCAL_ORCHESTRATOR=1` forces Cortex's agent loop to
   `http://localhost:2031/v1/agent-run`. This is a test path Snowflake left in
   the binary; we implement it, translating the Snowflake-shaped agent:run
   request to Ollama's `/api/chat` and streaming the reply back as the
   Anthropic-style SSE events (`content_block_start` → `message.delta` →
   `message.stop` → `[DONE]`) Cortex parses.
2. A fake `ollama` connection in `~/.snowflake/config.toml` with
   `authenticator = "PROGRAMMATIC_ACCESS_TOKEN"` points Cortex's Node SDK at
   `https://localhost:2443`. The proxy serves a minimal
   `/session/v1/login-request` stub so the SDK thinks auth succeeded, plus
   small stubs for heartbeat, token-refresh, queries, and telemetry.

With those in place, Cortex starts up, authenticates against the proxy, and
routes every agent turn to Ollama — no real Snowflake ever touched.

## Features

- **Text chat** — streams Ollama tokens back as `message.delta` text deltas.
- **Tool calling** — `read`, `write`, `edit`, `bash`, `grep`, `glob`, `web_fetch`,
  and all of Cortex's client-MCP tools (task, team, cron). Covers both
  structured tool_calls (when Ollama emits them) and qwen-style
  `<function=...>…</function>` text fallback — the proxy detects and rewrites
  either form into proper `tool_use` events.
- **Tool-result round-trip** — Cortex executes the tool client-side and
  replays the result as an assistant→tool message pair; the translator maps
  Cortex's nested `tool_use.*` / `tool_result.*` shapes to OpenAI chat
  `{role:"tool"}` messages.
- **Thinking-block stripping** — `<think>…</think>` content from Qwen/DeepSeek
  reasoning models is hidden from the user-visible output.
- **Update-robustness probe** — scans the installed Cortex binary for 15
  anchor strings the proxy depends on. Runs automatically at server startup
  and aborts if any required anchor has been renamed in a Cortex update.

## First-time setup

```bash
pixi run gen-cert          # self-signed cert for localhost HTTPS listener
pixi install               # installs the python env (~40 MB)
```

Add the ollama connection to `~/.snowflake/config.toml`:

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
