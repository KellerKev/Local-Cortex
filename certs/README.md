# certs/

Holds the self-signed TLS material the proxy uses for the HTTPS listener on
port 2443 (the Snowflake-auth path). The key/cert pair is **not** in version
control — generate your own on first clone:

    pixi run gen-cert

This writes `localhost.key` + `localhost.crt`, valid for `localhost` /
`127.0.0.1` for 10 years. Cortex accepts them because we set
`NODE_TLS_REJECT_UNAUTHORIZED=0` in `bin/cortex-ollama`.
