# Resident runtime

The resident runtime runs one harness job at a time for one workspace. It survives the terminal that started it and keeps job state in `.harness/runtime/resident.sqlite3`.

## Use

```text
harness daemon start
harness run --detach "Fix the parser and run its tests"
harness jobs list
harness jobs attach <job-id>
harness jobs cancel <job-id>
harness jobs resume <job-id>
harness daemon stop
```

`jobs attach` follows the durable event cursor. Use `--no-follow` to print available events and return. A stopped worker becomes `resume_ready` only when the normal harness database contains a valid run checkpoint. A worker that stops before that proof becomes `uncertain`; the daemon does not replay it.

## Steering messages

```text
harness jobs message <job-id> <node-id> "Check the Windows path case"
harness jobs receipts <job-id>
```

V1 queues messages for named nodes and records queued and delivered receipts. Limits are 8 KiB per message, 8 pending messages per target, 64 messages per job, and 3 messages per sender in a rolling three-second window. Messages cannot add tools, change the workflow graph, or execute code. Delivery occurs only at a model/node boundary when the workflow integration is enabled; it never interrupts a provider stream or file transaction.

## Security boundary

- The server binds to `127.0.0.1` only. It requires exactly one Host authority with the active port, one daemon token, and one canonical project identity header. Duplicate or malformed authorities fail authentication.
- The descriptor containing the port, token, and canonical project identity is written to `.harness/runtime/daemon.json` with owner-only permissions where the platform supports them. The SQLite database stores the same identity. Copying either file to another project does not grant control of the original daemon.
- Every mutation requires a client ID and command ID made from 1–128 ASCII letters, digits, dots, underscores, or hyphens. Credential-like IDs and mailbox senders are rejected before persistence through the shared credential redactor. The SQLite command journal returns the saved result for a completed duplicate. A received command with no saved result returns `command_result_uncertain`; it is never replayed.
- The API exposes jobs, events, cancel, resume, mailbox receipts, health, and shutdown. It has no shell, file, provider, Git, MCP, or arbitrary tool endpoint.
- Provider credentials remain in the daemon process environment. The descriptor, job database, and command journal do not store them. A task containing a configured credential value is rejected instead of retained.
- Each worker owns a process group on POSIX and a kill-on-close Job Object on Windows. Cancel and daemon shutdown terminate the worker tree. Commands started by the harness keep their existing per-command process-tree limits.
- Detached daemon and worker interpreters use isolated Python startup and an explicit canonical import root for the running package or ZIP archive. Changing to the project directory cannot redirect `our_harness` imports through the project or a relative `PYTHONPATH`.
- Only one daemon can own the workspace lock. It dispatches one worker at a time. Each attempt gets a random durable lease ID; worker updates must match the active lease.

## Crash rules

The resident database uses SQLite WAL transactions for jobs, events, commands, and mailbox receipts. The normal harness remains the authority for run checkpoints and file-transaction recovery.

Mailbox admission and delivery claims use write-reserved SQLite transactions. Concurrent producers cannot bypass total, per-target, or per-sender caps, and concurrent consumers cannot return the same queued message.

After a daemon or worker stop:

- a job with a retained run checkpoint becomes `resume_ready`;
- a job without a confirmed checkpoint becomes `uncertain`;
- `jobs resume` calls the existing `resume_task` path and uses its frozen graph, configuration hash, deadline, transaction journal, and compare-and-swap checkpoint;
- cancel uses the existing run cancellation path when a checkpoint exists;
- no job is blindly restarted.

## V1 limits

V1 has no global supervisor, scheduler, cron service, remote listener, dynamic agent creation, mid-token steering, or unrestricted project shell. The operating-system process cannot recover itself after a machine reboot; a later `daemon start` performs the durable queue audit. Hard power loss between mailbox receipt delivery and the provider request can consume a message without model acknowledgement; the receipt remains auditable.
