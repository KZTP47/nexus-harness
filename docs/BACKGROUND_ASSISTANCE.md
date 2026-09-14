# Background assistance and delivery visibility

Nexus can skip unnecessary scheduled checks, supplement project memory with local semantic matches, and show agent delivery stages. These additions do not pause agents, restart sessions, resend messages, change collaboration budgets, or change who can create and edit project files. Existing collaboration and recovery rules still apply.

## Scheduled input checks

When adding a timer, optionally list exact project-relative input files, one per line. A successful run records a versioned content observation. Later occurrences with identical inputs are marked **skipped**, without running the automation or sending a notification. Changed files, newly created or removed files, a different project root, and changed timer or automation settings invalidate that observation. Failed runs do not establish a new baseline.

The optional maximum lateness field skips a check when the latest scheduled occurrence is older than that many minutes. Zero disables lateness filtering. A missed week still uses the existing coalesced timer behavior; this does not create a backlog of runs. Manual runs remain manual.

Empty watched files and zero lateness preserve existing timer behavior, including accepted request IDs across upgrades. Watches are limited to 64 exact files and 4 MiB total. They do not recursively scan directories or follow links. If observation is unavailable, oversized, or detects a changing file, normal timer execution proceeds. Use this for periodic input checks; leave it empty for work that must run every time regardless of input changes.

## Optional local hybrid memory

Hybrid retrieval is off by default. With an already configured, project-bound memory vault and a locally installed Ollama embedding model, merge these fields into the existing `persistent_memory` object in trusted local configuration (`.harness/config.local.json`):

```json
{
  "persistent_memory": {
    "hybrid_search": true,
    "embedding_url": "http://127.0.0.1:11434",
    "embedding_model": "your-installed-embedding-model"
  }
}
```

Keep the existing vault binding and configuration fields. Nexus does not install models or change their lifetime settings. Only loopback HTTP endpoints are accepted; private note requests do not use environment proxies or redirects.

Search returns lexical matches immediately while a bounded daemon worker prepares embeddings. A cold process, new query, unavailable model, incompatible response, or busy semantic cache falls back to lexical search. Repeated searches for a prepared query can combine lexical and semantic rankings with reciprocal rank fusion. Preparation needs a running host, such as an ongoing Nexus workflow; a short-lived standalone hook can exit before its daemon worker finishes and remain lexical. The hook never waits for embeddings at shutdown. Mandatory project notes retain priority and the existing context budget. Evidence labels and retrieval metadata distinguish lexical, semantic, and combined matches.

Vectors are generated private state in the vault's existing SQLite index; Markdown remains canonical. Fingerprints include schema, chunking version, endpoint, model name and installed model digest, dimensions, vault path, project binding, and exact chunk content. Refresh invalidates edits and deletions, including edits with unchanged size and modification time. Model or binding changes during a batch discard the batch.

Work is limited to two embedding workers, 32 new chunks per preparation batch, 2,048 indexed candidate chunks, and 128 cached query vectors. Large vaults are therefore only partially covered by semantic retrieval; lexical search retains its existing scope. Tests establish retrieval mechanics with labeled synthetic vectors and a local HTTP fixture, not the relevance quality of a particular installed model.

## Agent delivery stages

The team exchange shows queued, dispatched, retrying, and response-recorded observations. Idle queued messages older than ten minutes or messages with recorded delivery failures get an attention label. An active agent is not declared stuck because it has been working for a long time. These labels do not retry, acknowledge, cancel, or interrupt anything.

Goal task cards distinguish a recorded response from a verified project result. Unknown outcomes retain the existing recovery path. Viewing status does not mutate durable goal or mailbox state.

## Verification

The regression fixture runs three substantive agent turns: one creates a project file, its peer edits that file and replies, and the first agent receives the reply and finishes. The same project has an active watched timer, and memory embedding is deliberately stalled throughout the exchange. Separate packaged acceptance covers team conversation, file publication, concurrent same-project chats, and restart recovery.
