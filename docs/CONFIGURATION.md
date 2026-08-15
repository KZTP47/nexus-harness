# Configuration

Effective settings use this order. Later layers win:

1. built-in defaults;
2. platform user config;
3. `.harness/config.json`;
4. `.harness/config.local.json`, only when its resolved project and file hash match the user trust record created by `harness init`;
5. an explicit `--config` file;
6. `HARNESS_*` environment variables;
7. command-line provider/model overrides.

User config locations:

- Windows: `%APPDATA%\our-harness\config.json`
- macOS and Linux: `${XDG_CONFIG_HOME:-~/.config}/our-harness/config.json`

`harness config` prints the effective value and source for each setting.

## Environment overrides

Short names:

```text
HARNESS_PROVIDER=ollama
HARNESS_MODEL=qwen2.5-coder:7b
HARNESS_ENDPOINT=http://127.0.0.1:11434
HARNESS_TIMEOUT_SECONDS=240
```

Any nested key can use double separators:

```text
HARNESS__WORKFLOW__MAX_ITERATIONS=5
HARNESS__EXECUTION__MODE=docker
HARNESS__MEMORY__EMBEDDING_MODEL=nomic-embed-text
HARNESS__UI__PORT=8890
```

Values parse as JSON booleans, null, integers, decimals, arrays, or objects when possible.

## Provider

Keep shareable model selection in `.harness/config.json`:

```json
{
  "provider": {
    "name": "openai",
    "model": "gpt-5",
    "api_mode": "auto",
    "prompt_cache_key": "",
    "prompt_cache_retention": "24h",
    "temperature": 0.2,
    "max_output_tokens": 8192,
    "timeout_seconds": 180
  }
}
```

Keep a credential-bearing or remote provider's complete route in the ignored `.harness/config.local.json`, a platform user config, environment overrides, an explicit trusted file, or command-line overrides. That includes its provider name and model as well as its endpoint and credential binding:

```json
{
  "provider": {
    "name": "openai",
    "model": "gpt-5",
    "endpoint": "https://api.openai.com/v1",
    "api_key_env": "OPENAI_API_KEY"
  }
}
```

For `name: "openai"`, `api_mode: "auto"` uses the Responses API. Set `api_mode` to `chat-completions` for the earlier endpoint. For `name: "openai-compatible"`, `auto` preserves Chat Completions behavior; set `responses` only when that endpoint implements the Responses contract. Ollama, Anthropic, and local-process adapters are unchanged by this setting.

### Provider profiles and agents

The legacy `provider` object remains valid. Use trusted `providers` and `agents` objects when different graph nodes need different routes:

```json
{
  "providers": {
    "planner_api": {
      "kind": "anthropic",
      "model": "claude-opus-5",
      "endpoint": "https://api.anthropic.com/v1",
      "api_key_env": "ANTHROPIC_PLANNER_KEY",
      "max_concurrency": 2,
      "allow_project_graphs": true,
      "max_data_class": "project_private"
    },
    "coder_local": {
      "kind": "ollama",
      "model": "qwen3-coder",
      "endpoint": "http://127.0.0.1:11434",
      "api_key_env": "",
      "max_concurrency": 1,
      "allow_project_graphs": true,
      "max_data_class": "restricted"
    },
    "review_api": {
      "kind": "gemini",
      "model": "gemini-3.6-flash",
      "endpoint": "https://generativelanguage.googleapis.com/v1beta",
      "api_key_env": "GEMINI_REVIEW_KEY",
      "allow_project_graphs": true
    }
  },
  "agents": {
    "planner": {"provider_ref": "planner_api", "role": "planner", "capabilities": ["workspace.read"]},
    "coder": {"provider_ref": "coder_local", "role": "coder", "capabilities": ["workspace.read", "workspace.write", "shell.execute"]},
    "reviewer": {"provider_ref": "review_api", "role": "reviewer", "capabilities": ["workspace.read"]}
  }
}
```

Each named profile reads only its `api_key_env`. It does not fall back to `HARNESS_API_KEY`. Provider profiles, agent definitions, and price data are rejected when they come from shareable project config. Put them in user config, a trusted `.harness/config.local.json`, or an explicit reviewed config.

`allow_project_graphs: true` lets a graph submitted by a project or the visual editor select that profile. Keep this opt-in in trusted config only. It grants that graph permission to send its selected state to the profile endpoint. Built-in workflows and the legacy `provider` route do not need this flag.

OpenAI, Anthropic, Gemini, Ollama, OpenAI-compatible, local-process, and optional Codex CLI profiles can coexist. Anthropic, Gemini, and Ollama retain provider-native tool state between a tool call and its result. Continuation state is bound to its provider and model. Gemini uses the Interactions API at `/v1beta/interactions`.

### Optional Codex CLI profile

A trusted local profile can delegate a structured turn to Codex using an existing ChatGPT sign-in. It does not use an OpenAI API key:

```json
{
  "providers": {
    "codex_subscription": {
      "kind": "codex-cli",
      "model": "gpt-5.6-sol",
      "command": ["codex"],
      "auth_mode": "chatgpt",
      "reasoning_effort": "high",
      "role_output_caps": {
        "planner": 2048,
        "coder": 4096,
        "evaluator": 2048,
        "merge": 2048
      },
      "allow_project_graphs": false,
      "max_data_class": "project_private",
      "timeout_seconds": 180
    }
  }
}
```

Put this profile in user config, a trusted `.harness/config.local.json`, or an explicitly reviewed config file. Shareable project config cannot enable it. Run `codex login` once, then run `harness doctor`. Doctor executes both `codex --version` and `codex login status`; finding a filename on `PATH` is not enough. In particular, a WindowsApps alias installed with the desktop app can be visible but refuse execution. Install an executable Codex CLI/runtime when doctor reports that error.

The adapter runs `codex exec` in an empty private temporary directory. Before each request, it asks that executable for `debug models --bundled`, validates the selected model, writes the emitted catalog unchanged to a mode-0600 temporary file, and passes it through `model_catalog_json`. This avoids parsing or changing another Codex installation's model cache. The adapter ignores user and project rules, selects the read-only sandbox, sends the prompt through stdin, requests a JSON Schema result, applies byte and wall-clock limits, validates the result again, and removes the temporary directory. `reasoning_effort` is passed as a fixed Codex config override. It does not read or copy Codex credentials. It does not expose native harness tools or continuation state.

ChatGPT plan, workspace, model, and rate limits still apply. A subscription run has no API price snapshot. Usage records set `cost_microusd` to `null` and `price_status` to `subscription-unpriced`; they never treat the call as free or apply API token prices. Do not copy account authentication into public CI. See the official [Codex authentication](https://developers.openai.com/codex/auth) and [non-interactive mode](https://developers.openai.com/codex/noninteractive) documentation.

Ollama uses its native function-tool protocol for both the legacy `provider` route and named profiles. The selected model must support tools. Ollama exposes model capabilities through `POST /api/show`. During streaming, the harness accumulates `thinking`, `content`, and `tool_calls` from every chunk, then sends the complete assistant message followed by `role: "tool"` results. This follows Ollama's [tool-calling](https://docs.ollama.com/capabilities/tool-calling), [streaming](https://docs.ollama.com/capabilities/streaming), and [model-details](https://docs.ollama.com/api-reference/show-model-details) contracts.

Set `workflow.require_executable_counterexamples` to `true` for strict evaluation. The planner must then express each counterexample as a direct public function call with literal arguments, or as `Input: <literal> should return <literal>` when one public target function exists. The evaluator runs those calls against a temporary copy of the changed files. Unsupported or failed evidence blocks a passing review. The default is `false`: safely parseable counterexamples still run, while older prose-only plans report `executable_coverage: "unsupported"` without breaking existing workflows.

`provider.timeout_seconds` is a wall-clock limit for one provider request. The remaining workflow deadline may lower it. It is not an inactivity timeout that resets after each streamed chunk. Slow local CPU models need enough `workflow.max_elapsed_seconds` for every planning, coding, and review round.

Named profiles may set `role_output_caps` for `planner`, `coder`, `evaluator`, and `merge`. Each value is an upper bound on that role's response tokens and cannot raise `max_output_tokens`. The harness applies these caps by graph role, not by provider or model name. Provider profiles remain trusted configuration.

For a named Anthropic profile, `prompt_cache_retention: "in_memory"` marks the fixed system prefix with Anthropic's five-minute ephemeral cache control. The changing context stays in a later uncached block. Anthropic profiles reject `24h`; Gemini, Ollama, local-process, and OpenAI-compatible profiles reject this OpenAI-oriented setting. Anthropic structured results use `output_config.format`, and tool inputs use `strict: true`.

The offline model list is onboarding metadata dated in code. It never makes a network request and it does not prove account access. Current model availability can differ by account. Official model lists: [OpenAI](https://developers.openai.com/api/docs/models), [Anthropic](https://platform.claude.com/docs/en/about-claude/models/overview), [Gemini](https://ai.google.dev/gemini-api/docs/models), and [Ollama local inventory](https://docs.ollama.com/api/tags).

### Usage and configured prices

Price data is user-owned. The harness does not hardcode prices because provider prices and long-context rules change. Store price snapshots in trusted config:

```json
{
  "pricing": {
    "allow_unpriced_remote_calls": false,
    "snapshots": [
      {
        "id": "openai-sol-2026-08",
        "provider": "openai",
        "model_pattern": "gpt-5.6-sol",
        "input_per_million_microusd": 5000000,
        "cached_input_per_million_microusd": 500000,
        "cache_write_per_million_microusd": 6250000,
        "output_per_million_microusd": 30000000,
        "effective_at": "2026-08-14",
        "source_url": "https://developers.openai.com/api/docs/models/gpt-5.6-sol"
      }
    ]
  }
}
```

Rates use micro-US-dollars per million tokens, so all arithmetic stays integer-based. Every usage record carries run, request, node, agent, role, profile, provider, model, token classes, latency, cost, and snapshot ID. Call `PriceCatalog.preflight` before a remote request. It fails when the route has no matching snapshot unless `allow_unpriced_remote_calls` is true. Ollama and local-process routes default to zero API cost. Codex CLI subscription routes are unpriced and keep cost null. Host compute cost is outside this counter.

During a Responses tool loop, the provider retains the response ID and every typed output item, including reasoning items. The official OpenAI path continues with `previous_response_id` and `function_call_output` items. An `openai-compatible` endpoint must opt into `api_mode: "responses"`; its continuation falls back to replaying the retained input and output items plus typed function results because the harness does not assume that a compatible server stores response state.

When the official OpenAI provider is forced to `api_mode: "chat-completions"`, native tool rounds retain the assistant `tool_calls` message and append one `role: "tool"` message with the matching `tool_call_id` for each result. The final request keeps the configured strict JSON schema. Generic compatible Chat Completions endpoints stay on the explicit action-envelope fallback because the harness does not assume equivalent native continuation behavior.

Planner, coder, repair, and reviewer calls include strict JSON Schemas when the selected API mode supports them. A refusal, incomplete generation, missing output text, malformed stream, or invalid JSON fails the workflow explicitly instead of being treated as a valid contract response.

The harness derives a stable cache key from the compiled instruction prefix when `prompt_cache_key` is empty. An explicit key must be at most 64 characters. `prompt_cache_retention` accepts an empty value, `in_memory`, or `24h`; OpenAI-specific cache options are not sent to generic Chat Completions endpoints. Run results report input, output, cached-input, and cache-write token counts when the provider returns them.

OpenAI references: [Responses API](https://developers.openai.com/api/docs/guides/migrate-to-responses), [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs), and [Prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching). The provider maps `prompt_cache_retention` from the harness config to the current `prompt_cache_options.ttl` request field.

### Trusted configuration boundary

The loader decides executable authority from the final value and its source after all layers are merged. A later trusted layer can replace an unsafe project value. A shareable `.harness/config.json` cannot leave any of these capabilities active:

- a credential-bearing or remote provider name, model, route options, endpoint, credential binding, or local provider command;
- an embedding provider or model, because enabling embeddings transmits project text to that effective route;
- MCP server definitions or plugin code;
- Docker mode, image, or network selection;
- extra inherited environment names;
- project test, lint, build, security, or performance command arrays unless a trusted layer approved the same final value;
- commit or push grants;
- disabling Git, lowering the trusted reviewer count or parallelism, disabling required review or rollback, or selecting a workflow policy that weakens those settings;
- higher byte, file-count, deadline, context, retention, iteration, or tool-call budgets than the trusted policy in effect before the project layer;
- removal of executable or argument denials established by defaults or user config.

Put those settings in `.harness/config.local.json` through `harness init`, or pass a reviewed file explicitly with `--config`. Ignoring a filename in Git does not make it trusted: a copied repository can contain both the file and an ignore rule. The user trust record is outside the project and binds the resolved project root to the local file hash and size. Editing the local file invalidates that record; review it and pass it explicitly or run init for a new project. Docker requires all three of `execution.mode`, `execution.docker_image`, and `execution.docker_network` to come from trusted config. This prevents a checked-out project from composing a trusted credential with its endpoint, selecting an implicit remote embedding route, inheriting another secret into child commands, selecting a local or remote MCP service, expanding resource safeguards, or reducing an existing command policy.

Remote provider and HTTP MCP URLs must use HTTPS. Plain HTTP is accepted only for loopback hosts (`127.0.0.1`, `localhost`, or `::1`). Runtime validation also applies the documented type, range, nested-object, and project-relative path constraints after all layers are merged.

Project-relative paths use a Windows-portable component contract on every platform. A component cannot end with a space or dot, contain a colon or alternate-data-stream suffix, or use a reserved DOS device basename such as `CON`, `NUL`, `COM1`, or `LPT9`, including names with extensions. Normalized `.git` and `.harness` components are reserved at every depth. File changes, discovery reads, indexing, and checkpoint restore all use the same validation before filesystem access.

`workflow.name` accepts `planner-coder-reviewer` or `gauntlet`. The Gauntlet policy validates its packaged graph and derives bounded repair settings from it. Syntax uses configured or detected lint commands. Security and performance use their own configured command groups and fail closed when absent. An enabled plugin may register another name with a workflow-policy factory. In the visual editor, **Simulate** is state-only; **Start run** submits the current graph to the production compiler, where tool roles and repair-loop values control real checks and limits.

`local` providers read one JSON request from stdin and must print one JSON object with a `text` field. Set `provider.command` to its argv array in trusted local or user configuration.

## Project commands

Detection runs during `harness init`. Detected commands are written only to ignored `.harness/config.local.json`. Shareable config cannot introduce executable command arrays. Explicit trusted commands take priority later:

```json
{
  "project": {
    "test_commands": [["python", "-m", "pytest", "-q"]],
    "lint_commands": [["python", "-m", "ruff", "check", "."]],
    "build_commands": [],
    "security_commands": [["python", "-m", "bandit", "-r", "src"]],
    "performance_commands": [["python", "benchmarks/smoke.py"]]
  }
}
```

Commands are argv arrays. They do not use a shell unless an explicit tool executable starts one.

## Memory

```json
{
  "memory": {
    "enabled": true,
    "database": ".harness/memory/harness.db",
    "max_results": 8,
    "retention_days": 180,
    "embedding_provider": "",
    "embedding_model": "",
    "allow_remote_embeddings": false
  }
}
```

Set `embedding_model` in trusted configuration to activate vector creation and query scoring. `embedding_provider` selects the provider for both workspace and episodic vectors; an empty value uses `provider.name`. A non-loopback route also requires trusted `allow_remote_embeddings: true`. The provider, model, and effective endpoint determine where project chunks are sent, so shareable project config cannot enable or route embeddings. `harness index` reports the route class and selected file paths. FTS and dependency retrieval work without embeddings.

When `memory.enabled` is `false`, the harness does not create the configured database, scan or index project source, retain or retrieve episodes, or retain run history, review packets, and refinement versions. The current run still uses an in-process SQLite store for ordered workflow events, which disappears when the process exits. `harness index` reports zero files, memory searches return no results, and retained-memory/refinement mutation commands fail with an explanation.

## Execution

```json
{
  "execution": {
    "mode": "process",
    "timeout_seconds": 180,
    "max_output_bytes": 250000,
    "max_changed_files": 24,
    "max_changed_bytes": 2000000,
    "inherit_environment": ["PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "LANG", "LC_ALL"],
    "deny_executables": ["format", "diskpart", "shutdown", "reboot"],
    "deny_argument_sequences": ["--force", "reset --hard", "clean -fd", "push --force"],
    "docker_image": "python:3.12-slim",
    "docker_network": "none"
  }
}
```

The denied list is a final guard, not a full policy language. Shareable config may add denials but cannot remove defaults or entries from user config. A trusted later layer may replace the policy explicitly. Use Docker or another OS isolation layer for untrusted code.

`execution.timeout_seconds` is an end-to-end command deadline. It covers process startup, the foreground process, stdin delivery, and stdout/stderr drain. Windows runs each command in a kill-on-close Job Object. POSIX runs it in a new process group. When the deadline expires, the runner kills the owned tree, including descendants that keep output pipes open after the parent exits. The runner also closes the tree after a successful foreground command so background descendants do not escape command scope.

## Workflow

```json
{
  "workflow": {
    "name": "planner-coder-reviewer",
    "max_iterations": 4,
    "max_elapsed_seconds": 1800,
    "repeat_failure_limit": 2,
    "max_tool_calls": 12,
    "max_tool_output_bytes": 32000,
    "max_tool_total_bytes": 128000,
    "reviewers": 2,
    "review_parallelism": 2,
    "reviewer_lenses": ["correctness", "counterexample"],
    "temperature_decay": 0.75,
    "rollback_on_exhaustion": true,
    "require_review": true
  }
}
```

`workflow.max_elapsed_seconds` is one deadline for discovery, retrieval, provider calls, commands, review, and persistence. Provider, discovery-tool, MCP, and command adapters receive the smaller of their configured timeout and the remaining workflow time.

`workflow.max_tool_calls` bounds planner/coder discovery calls across the entire run. `max_tool_output_bytes` is the per-result serialized limit and `max_tool_total_bytes` is the run-wide result limit. Duplicate calls count toward the call limit and reuse the first bounded result. Completed results are journaled by run, node, call ID, tool, and canonical argument hash. A resumed node reuses an exact matching journal entry and rejects call-ID rebinding.

`workflow.reviewers` and `workflow.review_parallelism` each accept 1 through 5. `reviewer_lenses` is optional; when present, it must contain one unique plain name per reviewer. A value above one runs the production evaluator as an independent panel. Each member gets a separate provider instance, immutable lens policy, the same canonical evidence packet, and the same absolute deadline. The run records per-reviewer latency and token usage, isolates failures, and passes only when every required reviewer passes. A value of one keeps the single isolated reviewer path. Shareable project config cannot turn a trusted `require_review` or `rollback_on_exhaustion` value from `true` to `false`; a later trusted layer may do so explicitly.

### Hard resource ceilings

All configuration layers, including trusted local overrides, remain inside finite hard ceilings. The main ceilings are: provider and command timeouts 3,600 seconds; workflow elapsed time 86,400 seconds; provider output 1,000,000 tokens; per-command output, project files, and MCP responses 100,000,000 bytes; cumulative changed bytes 1,000,000,000; context fields 10,000,000 characters; memory retention 3,650 days; and the existing tool-loop, iteration, reviewer, and result-count maxima in `harness.schema.json`. Shareable project config may lower byte, deadline, retention, context, iteration, and tool budgets, but it cannot raise the trusted values it inherited. It may raise reviewer count or parallelism but cannot lower the trusted floor; both retain their hard maximum of five.

## Git

Commits and pushes are off by default.

```json
{
  "git": {
    "enabled": true,
    "allow_commit": false,
    "allow_push": false,
    "allow_merge": false,
    "protected_branches": ["main", "master"],
    "required_branch_prefix": ""
  }
}
```

The Git adapter stages only an explicit path list and checks that the actual staged set matches it. It never merges or force-pushes. Shareable project config may add protected branches or narrow an existing required branch prefix. It cannot remove a branch protected by defaults or user config, clear a trusted prefix, or replace it with an unrelated prefix. A later trusted layer may replace either policy explicitly.

## MCP

See [MCP.md](MCP.md). Servers are disabled unless listed in config. Each server accepts `protocol_mode: "legacy" | "auto" | "modern"`; omitted means `legacy`. Planner and coder discovery accepts an allowlisted MCP tool only when `tools/list` sets `annotations.readOnlyHint` to exactly `true` without `destructiveHint: true`. Idempotence alone grants no discovery authority.

## Schema

`harness.schema.json` defines the public config shape. The runtime rejects unknown keys and unsupported schema versions.
