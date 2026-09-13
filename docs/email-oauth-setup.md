# Email sign-in: application-owner setup

Verified against official provider documentation on 2026-09-12.

The intended user experience is **Connect Outlook** or **Connect Gmail**, then
account selection and consent in the system browser. Users should not create
cloud projects, paste refresh tokens, or discover mail-server settings for the
normal connection path. An organization may still require administrator
approval. Manual connection settings belong in the advanced fallback.

Before distributing that experience, the application owner must register real
OAuth clients and supply their identifiers to the release. This document does
not supply invented client IDs or claim that an app registration or provider
verification has already been completed.

## Microsoft Outlook and Microsoft 365

1. In the Microsoft Entra admin center, create an app registration owned by the
   product publisher. Select accounts in **any organizational directory and
   personal Microsoft accounts**, represented by
   `AzureADandPersonalMicrosoftAccount`. This covers Microsoft 365 work/school
   accounts and Outlook.com consumer accounts with one registration. Use the
   `common` authority for this audience, rather than a tenant-only authority.
   [Account audiences](https://learn.microsoft.com/en-us/entra/identity-platform/supported-accounts-validation),
   [multitenant authority](https://learn.microsoft.com/en-us/entra/identity-platform/howto-convert-app-to-be-multi-tenant).
2. Under Authentication, add the **Mobile and desktop applications** platform.
   For the system-browser flow, Microsoft documents `http://localhost` as the
   desktop redirect. Configure a public/native client with authorization-code
   flow and PKCE; do not put a confidential-client secret in the desktop app.
   [Desktop configuration](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration),
   [authorization-code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow).
3. Register the same callback path the implementation uses. Microsoft ignores
   the port when matching `localhost` redirects, allowing a random local port,
   but the path still matters. Bind the listener to loopback only. Do not
   assume a literal `127.0.0.1` redirect has identical registration behavior:
   Microsoft's portal requires manifest editing for HTTP IP-literal redirects,
   and its documented port exception is for `localhost`. IPv6 literal redirect
   support also differs. Verify the exact chosen redirect with a real login.
   [Redirect matching rules](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url).
4. Add only the Microsoft Graph **delegated** permissions required by the
   implemented features:

   | Feature | Permission |
   | --- | --- |
   | Read incoming bodies | `Mail.Read` |
   | Create/update drafts in Outlook itself | `Mail.ReadWrite`, replacing read-only access |
   | Send the approved reply | `Mail.Send` |
   | Obtain profile data through Graph `/me` | `User.Read`, if that endpoint is used |

   `Mail.ReadBasic` does not include message bodies. `Mail.ReadWrite` does not
   grant sending. The listed mail permissions support delegated personal-account
   consent; organization policies can still restrict consent. Local-only draft
   editing does not itself require mailbox write permission.
   [Graph permission reference](https://learn.microsoft.com/en-us/graph/permissions-reference).
5. Request `offline_access` to receive refresh tokens; request OIDC identity
   scopes only when their claims are used. Publish the application/client ID
   as public configuration, not as a user secret.
   [OIDC scopes and offline access](https://learn.microsoft.com/en-us/entra/identity-platform/scopes-oidc).

Configure publisher identity, support and privacy information before public
distribution. Publisher verification materially improves organizational
consent: risk-based policies can block consent to newly registered, unverified
multitenant apps requesting mail access. Verification is not a guarantee that
every organization's administrator will permit access.
[Publisher verification](https://learn.microsoft.com/en-us/entra/identity-platform/publisher-verification-overview).

## Gmail

1. Create an owner-controlled Google Cloud project and enable the Gmail API.
   Configure Google Auth Platform branding, audience, support contact, homepage,
   privacy policy and requested data access. Choose External for a public app
   serving consumer Gmail and multiple organizations; Internal is for the
   owner's eligible organization, not arbitrary customers.
2. Create an OAuth client of type **Desktop app**. Use the system browser with
   authorization code, PKCE `S256`, and a loopback listener on a random available
   port, for example the provider-supported `http://127.0.0.1:<port>` form.
   Preserve the exact redirect through authorization and token exchange.
   Installed apps cannot keep client secrets confidential. Google's native
   token-exchange documentation marks `client_secret` optional; use the actual
   desktop-client configuration and never substitute a web-client secret.
   [Native-app OAuth setup and protocol](https://developers.google.com/identity/protocols/oauth2/native-app).
3. Declare the smallest scope set matching the actual connector:

   | Feature | Scope suffix under `https://www.googleapis.com/auth/` | Classification |
   | --- | --- | --- |
   | Read incoming messages | `gmail.readonly` | Restricted |
   | Send the approved reply | `gmail.send` | Sensitive |
   | Maintain drafts in Gmail itself | `gmail.compose` | Restricted |

   Reading mail plus local draft review and sending needs read-only and send
   access; it does not inherently need `gmail.modify`. Metadata-only access
   cannot retrieve bodies. Do not request `https://mail.google.com/` for this
   feature; that scope also permits permanent deletion.
   [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes).

During external Testing, add the intended testers to the allowlist; the normal
limit is 100 test users. Public verified distribution is a separate step.
Workspace administrators retain the power to block even verified apps.
[App publication states](https://developers.google.com/identity/protocols/oauth2/production-readiness/overview).
Testing refresh tokens ordinarily expire after seven days when Gmail scopes
are requested; do not describe a test build as permanently connected.
[Refresh-token expiration](https://developers.google.com/identity/protocols/oauth2#expiration).

For public Gmail-reading distribution, submit the restricted scopes for
verification unless a documented exception applies. Prepare scope justification,
the real user flow, accurate branding and data-use documentation. Apps accessing
restricted data through third-party servers require an assessment under Google's
published verification rules. **Local Electron packaging alone does not make
this app exempt**: sending email text to a remote AI service changes the data
flow. The owner must determine the applicable verification/assessment requirements
for the actual deployment before advertising unrestricted public availability.
[Restricted-scope verification](https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification).

## Data handling and release review

Google requires disclosure and consent for the actual use and sharing of
Workspace data, encrypted tokens and user data at rest, deletion controls, and
restricted use of data for AI. Learning for the specific user's visible feature
is different from improving general-purpose models. The current policy also
warns against permanent copies of Google user data. Do not infer that a Gmail
grant authorizes indefinite raw-message archival: review retention, cached
content, extracted preferences, and AI-provider terms for the intended product.
This is an external launch dependency, not a claim that the current storage
implementation has passed Google's review.
[Workspace user-data policy](https://developers.google.com/workspace/workspace-api-user-data-developer-policy).

Implementation acceptance checks:

- Bind the callback listener before opening the browser. Use independent,
  single-use state and PKCE values per attempt; expire and close abandoned
  listeners. Reject wrong state, duplicate callbacks and unexpected paths.
- Keep tokens in the backend's encrypted credential store. Do not expose codes,
  refresh tokens or verifiers in renderer responses, callback pages, logs,
  command lines, exported diagnostics, or Kestra flow inputs.
- Bind credentials and retrieved mail to provider plus verified account ID;
  changing accounts must not mix inboxes or learned preferences. Handle token
  rotation, revocation and reconnect without claiming a live connection early.
- Use the external system browser. Google disallows OAuth inside a developer-
  controlled embedded user-agent.
  [Google OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies).
- Test cancellation, denied scopes, expired/revoked grants, administrator
  restrictions, two parallel sign-ins, restart, and fresh-machine installation.
  Then prove real Microsoft and Google logins using the owner's registered
  clients. Mock OAuth tests do not prove registration or verification readiness.

Until real client IDs and external approvals are available, the UI must state
that the publisher has not configured that provider. It should not send the
user to a nonfunctional login URL or make advanced setup look mandatory for
normal users of the eventual published app.


## Where Nexus reads release registrations

`src/our_harness/email_oauth_defaults.py` is the product-owned release configuration, bundled into the desktop Python runtime and available to every project. Its client IDs are deliberately empty until the publisher registers Nexus. Microsoft should register `http://localhost/callback`; Google uses the Desktop client with an ephemeral loopback `/callback` address. The connector supplies the random port at runtime.

For a private managed deployment, `email_oauth.clients.outlook` and `email_oauth.clients.gmail` in project configuration override release defaults. The advanced UI can also save a local override; those values are encrypted with Windows DPAPI in the project's private mail directory. Never place mailbox access or refresh tokens in product defaults or project configuration. Google may supply a desktop client value named `client_secret`; it is optional for this public-client implementation and is never a replacement for PKCE.

Local tests use injected synthetic provider responses and real loopback callbacks. They verify the software protocol and account boundaries, not Microsoft's or Google's acceptance of an unregistered app. A release with effortless end-user setup remains blocked until real registrations and applicable provider review are completed.
