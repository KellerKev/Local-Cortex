"""JSON schemas for Cortex Code's built-in tools.

Cortex sends these as bare `{type, name}` entries — without input_schema — on
the assumption that the hosted orchestrator already knows their shape. For the
local Ollama path we have to supply schemas ourselves so the model can invoke
the tool correctly.

Schemas mirror Cortex Code's public tool semantics (same tool family as Claude
Code). Keep them permissive: we only need enough shape for the model to fill
the right fields; Cortex validates and executes the call client-side.
"""

from __future__ import annotations

from typing import Any


def _obj(**props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": dict(props)}


BUILTIN_SCHEMAS: dict[str, dict[str, Any]] = {
    "read": {
        "description": "Read the contents of a file at an absolute path. Supports optional offset/limit for partial reads.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file."},
                "offset": {"type": "integer", "description": "1-based line to start from."},
                "limit": {"type": "integer", "description": "Max number of lines to read."},
            },
            "required": ["file_path"],
        },
    },
    "write": {
        "description": "Write (or overwrite) a file with the given content at an absolute path.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        },
    },
    "edit": {
        "description": "Replace an exact string in a file with a new one. Use replace_all for global replacement.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    "bash": {
        "description": "Run a shell command. Set run_in_background for long-running work; poll with bash_output.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "description": {"type": "string"},
                "timeout": {"type": "integer", "description": "Milliseconds."},
                "run_in_background": {"type": "boolean"},
            },
            "required": ["command"],
        },
    },
    "bash_output": {
        "description": "Fetch new stdout/stderr from a background bash command by shell id.",
        "input_schema": {
            "type": "object",
            "properties": {"bash_id": {"type": "string"}},
            "required": ["bash_id"],
        },
    },
    "kill_shell": {
        "description": "Terminate a running background bash command by shell id.",
        "input_schema": {
            "type": "object",
            "properties": {"shell_id": {"type": "string"}},
            "required": ["shell_id"],
        },
    },
    "grep": {
        "description": "Search file contents with a regex. Defaults to the current working tree.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
                "glob": {"type": "string"},
                "output_mode": {"type": "string", "enum": ["content", "files_with_matches", "count"]},
                "-i": {"type": "boolean"},
                "-n": {"type": "boolean"},
                "-A": {"type": "integer"},
                "-B": {"type": "integer"},
                "-C": {"type": "integer"},
                "multiline": {"type": "boolean"},
                "head_limit": {"type": "integer"},
            },
            "required": ["pattern"],
        },
    },
    "glob": {
        "description": "Find files by glob pattern (e.g. \"**/*.py\").",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "path": {"type": "string"},
            },
            "required": ["pattern"],
        },
    },
    "web_fetch": {
        "description": "Fetch a URL and process the content against an inline prompt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "prompt": {"type": "string"},
            },
            "required": ["url", "prompt"],
        },
    },
    "web_search": {
        "description": "Perform a web search and return result summaries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "allowed_domains": {"type": "array", "items": {"type": "string"}},
                "blocked_domains": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
        },
    },
    "data_diff": {
        "description": "Diff two data artifacts (files, tables, or query outputs).",
        "input_schema": {
            "type": "object",
            "properties": {
                "left": {"type": "string"},
                "right": {"type": "string"},
            },
            "required": ["left", "right"],
        },
    },
    "notebook_actions": {
        "description": "Manipulate a Jupyter notebook (list/add/edit/delete cells).",
        "input_schema": {
            "type": "object",
            "properties": {
                "notebook_path": {"type": "string"},
                "action": {"type": "string"},
                "cell_id": {"type": "string"},
                "cell_type": {"type": "string"},
                "source": {"type": "string"},
            },
            "required": ["notebook_path", "action"],
        },
    },
    "snowflake_sql_execute": {
        # Cortex 1.0.48 name. Kept for backward-compat. Cortex 1.0.73+ uses
        # the shorter `sql_execute` (renamed when Postgres support was added).
        "description": "Execute a Snowflake SQL statement. Returns rows in a structured format.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "The SQL query or queries to execute. Multiple statements separated by semicolons.",
                },
                "description": {
                    "type": "string",
                    "description": "User-facing summary of what this query does, under 30 words.",
                },
                "connection": {
                    "type": "string",
                    "description": "Optional connection name. If not specified, uses the currently active SQL connection.",
                },
                "timeout_seconds": {"type": "number"},
                "only_compile": {"type": "boolean"},
            },
            "required": ["sql", "description"],
        },
    },
    "sql_execute": {
        # Cortex 1.0.73+ name. Same wire shape as snowflake_sql_execute, but
        # also accepts Postgres connections. Required-fields list matches the
        # live binary (sql + description) so the model fills both.
        "description": "Execute SQL queries against the active SQL connection. Supports Snowflake and Postgres targets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": "The SQL query or queries to execute. Multiple statements separated by semicolons.",
                },
                "description": {
                    "type": "string",
                    "description": "User-facing summary of what this query does, under 30 words.",
                },
                "connection": {
                    "type": "string",
                    "description": "Optional connection name. If not specified, uses the currently active SQL connection.",
                },
                "timeout_seconds": {"type": "number"},
                "only_compile": {"type": "boolean"},
            },
            "required": ["sql", "description"],
        },
    },
    "fdbt": {
        "description": "Run an fdbt (Snowflake dbt) command.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
    "ask_user_question": {
        "description": "Ask the user a clarifying question before proceeding.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["question"],
        },
    },
    "enter_plan_mode": {
        "description": "Enter plan mode to propose a multi-step plan before making changes.",
        "input_schema": {
            "type": "object",
            "properties": {"plan": {"type": "string"}},
        },
    },
    "exit_plan_mode": {
        "description": "Leave plan mode and resume normal execution.",
        "input_schema": {"type": "object", "properties": {}},
    },
    "send_message": {
        "description": "Send a message to another agent in the same team.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["to", "message"],
        },
    },
    "skill": {
        "description": "Invoke a named skill that has been registered with Cortex.",
        "input_schema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string"},
                "args": {"type": "string"},
            },
            "required": ["skill"],
        },
    },
}


def to_openai_tools(cortex_tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Cortex's `tools` array → OpenAI/Ollama tool-calling schema.

    Cortex tool entries are `{tool_spec: {type, name, [description, input_schema]}}`.
    MCP-backed tools already carry a full `input_schema`; bare built-ins we fill
    from BUILTIN_SCHEMAS. Unknown tools get a permissive empty-object schema.
    """
    out: list[dict[str, Any]] = []
    for entry in cortex_tools:
        spec = entry.get("tool_spec") or {}
        name = spec.get("name")
        if not name:
            continue
        description = spec.get("description")
        input_schema = spec.get("input_schema")
        if input_schema is None:
            fallback = BUILTIN_SCHEMAS.get(name)
            if fallback:
                description = description or fallback["description"]
                input_schema = fallback["input_schema"]
            else:
                input_schema = {"type": "object", "properties": {}}
                description = description or f"Cortex built-in tool: {name}"
        out.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description or name,
                    "parameters": input_schema,
                },
            }
        )
    return out
