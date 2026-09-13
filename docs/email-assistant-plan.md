# Email assistant implementation and acceptance

This document describes product contracts and reproducible verification. Local
mailbox observations, correspondence, session identifiers and developer receipts
belong in private runtime records and are not publication material.

## Product contracts

- Mailbox ownership binds messages, drafts, browser profiles and preferences to
  a versioned account/configuration fingerprint. Reconnection and changed
  identities cannot mix correspondence or reuse obsolete authorization.
- Browser, classic Outlook, registered API and EmailEngine adapters expose
  distinct capabilities. Browser and API sends require explicit approval of
  the exact reviewed reply. Classic Outlook exports approved replies.
- Provider failure and ambiguous submission retain factual state and prevent
  blind duplicate sends. Restart recovery preserves pending work and ownership.
- Whole-second polling settings persist and reschedule promptly. Scans never
  overlap. Browser caches prioritize changed rows and periodically reconcile
  unchanged visible rows; provider and generation latency add to polling time.
- AI connection/model selection belongs to the email workspace. Changed routes
  invalidate stale execution state without discarding the user's revision text.
- Manual preferences remain editable. Automatic revision learning records
  recipient-scoped outcomes, including empty or failed extraction, separately
  from approval and delivery. Retry learning does not modify or send a reply.
- Reply-To takes precedence over sender. Ambiguous recipients fail closed.
  Recipient/mailbox/schema/contract boundaries survive restart and migration.
- Explicit recurring template requests may include omissions and replacement
  wording. Clauses from one extraction persist together; a later request may
  supersede the category. Recipient overrides take precedence over mailbox-wide
  defaults, while one-off facts remain ineligible.
- The packaged product includes verified runtime dependencies and excludes
  correspondence, credentials, profiles, local configuration and bytecode caches.

## Reproducible verification

Run `python -m unittest discover -s tests -p 'test_email*.py'` for email domain,
service, connector, persistence, retry, configuration and recipient boundaries.
Run `npm test` in `desktop` for renderer, browser worker, navigation, export and
send-control contracts. Fixtures use synthetic identities and temporary roots.

The `email-*.smoke.js` scripts exercise packaged UI behavior in isolated profiles.
`NEXUS_TEST_REAL_KESTRA=1` and `NEXUS_TEST_KESTRA=1` enable bundled-engine checks;
`python scripts/email_provider_smoke.py --live` uses synthetic mail with a
configured provider and does not send correspondence. Live provider checks are
separate from offline regression coverage.

## Continuing acceptance boundaries

API sign-in requires legitimate publisher registrations and provider consent.
Browser readers support bounded rendered conversations, not exhaustive mailbox
history or every language/layout. Classic Outlook requires its installed COM
interface. Background operation requires the app and computer to remain running.
Real provider token refresh/revocation, recipient delivery, sleep/wake behavior
and long-running mailbox operation require explicit integration acceptance.
See [Email assistant](email-assistant.md) and
[architecture options](email-architecture-options.md) for capability limits.
