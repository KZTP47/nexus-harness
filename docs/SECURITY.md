# Security

## Trust boundary

The harness can execute model-proposed code and commands with the current user account. The default runner is not a security sandbox.

Use a dedicated account, virtual machine, container, or OS sandbox for code or instructions you do not trust.

## Built-in controls

- project-relative path requirement;
- resolved-parent containment checks;
- symlink and Windows reparse-point rejection;
- exact baseline hashes before writes;
- changed-file and changed-byte limits;
- atomic same-directory replacement;
- a cross-process project transaction lock held for the workflow;
- identity and hash revalidation immediately before backup and replacement;
- manifest-bound backup hash and size verification for the full restore set before rollback writes;
- rollback refusal after later changes;
- packet-only reviewer requests under a separate immutable policy;
- final frozen-scope verification around completion persistence and return;
- argv execution with `shell=False`;
- filtered inherited environment with final-source checks that prevent shareable config from adding names;
- an end-to-end process deadline, Windows kill-on-close Job Object ownership, and POSIX process-group termination;
- output byte cap enforced while stdout and stderr are drained;
- read-only planner/coder discovery tools with root, call, byte, and deadline bounds;
- tool outputs labelled as untrusted data and recorded with provenance;
- no shell or file-write operation in the discovery loop;
- denied executable and argument patterns;
- loopback-only UI;
- one active UI-initiated run per workspace server;
- per-session UI mutation token;
- loopback Host authority checks before routing, same-origin request metadata checks, and token checks for mutation and event reads;
- request body cap and content security policy;
- MCP tool allowlists, timeouts, and response caps;
- redirect refusal for provider and MCP HTTP requests so credentials and tool authority stay on the configured endpoint;
- Git commits and pushes disabled by default;
- protected-branch checks and exact staged-path comparison.

## Secrets

Config stores environment-variable names. It must not store credential values. The UI does not expose provider keys. A central redactor removes configured or environment credential values, common token forms, private-key blocks, bearer values, and credential fields before provider requests and persistent memory writes. Redaction is a safety boundary, not a reason to pass credentials in prompts. Use limited credentials and review exported artifacts.

Workspace indexing, context standards, and agent `list_tree` and `read_file` use one visibility policy. It applies root and nested `.gitignore`, `.ignore`, and `project.ignore` glob rules. It also excludes `.env*`, credential files, SSH key names, private-key files, key stores, and certificate files before reading them. Ignore negation cannot re-enable built-in secret exclusions or a directory whose excluded parent was not re-included. Index reports list the files selected for embeddings so a remote opt-in can be audited.

Run checkpoints, review packets, run results, tool journals, episodes, indexed chunks, and refinement records pass through the same credential redactor before canonical hashing or storage. Checkpoint resume uses the redacted canonical task and state.

The final config provenance check prevents shareable project config, including an unrecorded `config.local.json`, from selecting or configuring a credential-bearing or remote provider, enabling or routing embeddings, binding a credential name, adding inherited environment names, defining MCP servers, enabling plugin code, supplying new command arrays, or granting Docker and Git authority. It also prevents a project layer from disabling trusted Git, lowering reviewer policy, weakening required review or rollback, removing command denials, or expanding inherited byte, deadline, context, retention, iteration, and tool budgets. Independent hard ceilings still apply to trusted configuration. Put capability grants in a hash-bound local config, user config, environment overrides, an explicit trusted file, or command-line overrides.

## MCP

MCP server schemas describe inputs; they do not prove the server is safe. Treat each server as code with the user account's authority. The planner/coder loop exposes an MCP server only when `allowed_tools` is non-empty and rejects any tool not explicitly named there. Allowlisting a mutating MCP tool grants that tool's authority; prefer read-only server operations for discovery.

## Docker mode

Docker mode mounts the project into `/workspace`. Its default network is `none`. The mount remains writable because coding tasks need file changes. Choose an image with the required runtime and do not treat container use as a complete defense against a hostile kernel or daemon.

## Persistent programmatic workspace

The V1 programmatic workspace persists an authenticated staged-file checkpoint, not a running interpreter. A per-user 256-bit HMAC key stays outside the project. The checkpoint binds the canonical project path and directory identity, effective configuration, verification authority, source type, mode, content, filesystem identity, action intent, stage lease, and completion journal. A project copy cannot forge a checkpoint or transplant an authenticated candidate from another project. An interrupted action remains uncertain and cannot run again under the same session.

The interface exposes typed file actions and named verification actions only. Verification argv comes from trusted harness configuration; a model cannot add a command through this interface. Persistent verification requires `execution.mode: docker`. The container mounts the temporary stage only. Host-process mode is rejected because a working-directory boundary cannot stop tested code from opening another host path. A full-project identity guard must also remain unchanged before a verification result is accepted. The same file, byte, tool-call, output, process, and workflow-deadline limits used by staged coding still apply.

Project files remain unchanged until the caller passes the final candidate to `FileTransaction`. Restore revalidates source identity and the exact authority specification, then requires fresh verification. Checkpoints contain the changed source needed for restart, encoded in a bounded envelope; encoding is not encryption. Known credential material is refused before persistence. Protect `.harness` and the per-user key with normal local filesystem permissions and do not place secrets in generated source.

An abrupt process termination releases the stage's operating-system file lock. The controller first reserves an empty private stage directory, writes its bounded HMAC-signed record at the deterministic project/session registry path, and acquires the OS lease. Only then may source or support bytes be copied into the stage. The next constructor reads only that record, authenticates its project, session, exact stage path, nonce, and creation time, confines the stage to the direct operating-system temp root, and acquires the released lease before deletion. This covers crashes during initial creation and during restore replacement, including termination before the first copy or checkpoint. A live lease blocks cleanup. Malformed files, links, and unrelated registry entries remain untouched. The durable action intent prevents a verification with external effects from being repeated after termination.

## Git

The adapter does not merge, force-push, or infer publication approval. Enabling `allow_commit` or `allow_push` grants only that named operation. Repository host actions need a separate plugin or manual step.

## UI

The graph server accepts only `127.0.0.1`, `localhost`, or `::1`. Every request must carry exactly one loopback `Host` authority with the active server port before routing begins. Browser request metadata must be same-origin. The bootstrap response is available only through that checked authority; event reads and mutations also require the session token. This blocks a non-loopback DNS name from reading local API data after resolving to loopback. CLI clients may omit browser-only `Origin`, `Referer`, and `Sec-Fetch-Site` headers, but must send the checked Host authority and use the bootstrap token for event reads or mutations. Do not place the server behind a public proxy. The token is not user authentication.

## Reporting

When reporting a security issue, include the version, OS, command, affected path class, and a minimal reproduction without credentials or private source.
