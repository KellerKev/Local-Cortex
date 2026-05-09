---
description: Show which local Ollama model the cortex_ollama proxy is currently routing to.
allowed-tools: bash
hide-from-slash-command-tool: true
---

Run this bash command exactly once and report the raw JSON to me with no commentary:

```
curl -s http://127.0.0.1:2031/healthz
```

The `model` field in the response is the local Ollama model serving every agent
turn in this session. Also list the other locally-installed models so I know
what I can swap to:

```
curl -s http://127.0.0.1:2031/models
```
