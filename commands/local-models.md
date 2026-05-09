---
description: List all local Ollama models available to swap into.
allowed-tools: bash
hide-from-slash-command-tool: true
---

Run this bash command exactly once and show me the JSON verbatim:

```
curl -s http://127.0.0.1:2031/models
```

`current` is the model serving this session. `available` is the full list. To
switch mid-session, run from another terminal:

```
curl -X POST http://127.0.0.1:2031/model -H 'Content-Type: application/json' -d '{"model": "<name-from-available>"}'
```

The next agent turn will use the new model.
