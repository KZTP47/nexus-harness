# Collaboration discussion audit — v0.2.23

The later discussion replaced a compulsory writer/reviewer handoff with a user
choice between fixed roles and flexible collaboration. It also explicitly accepted
closeout enforcement as distinct from preventing every native side effect.

| Discussed outcome | Audit finding and resulting implementation | Evidence owner |
| --- | --- | --- |
| Native document, ZIP and GitHub skill work | Previously implemented; retained in this release | Document, research and provider tests |
| Real project plus independent agent copies, visible to the user | Previously implemented; retained workspace viewer | Agent workspace and desktop viewer tests |
| Reviewer sees the author's runnable submission | Missing from ordinary reviews; now uses an authenticated snapshot of the accepted base plus the author's exact changes | Workspace collaboration tests |
| Flexible inspection, edits and testing without compulsory handoff | Missing; added goal-owned workspace tools and native workspace context | Workspace collaboration and context continuation tests |
| UI choice of fixed roles or flexible collaboration | Missing; added persistent chat preferences and revision-checked goal settings | Browser UI and role-policy tests |
| Separate permission for direct real-project editing | Missing; added explicit checkbox, default off | Direct-edit positive and denied-write tests |
| Closeout checks, independent judge, repairs and repeat limits | Previously implemented; extended to the selected collaboration rules | Closeout and shared-chat graph tests |
| Judge and repair agent remember the whole original request | Previously implemented; retained original prompt, amendments, full criteria and complete findings across restart | Closeout scope and restart tests |

Fixed roles enforce Nexus file proposals, workspace tools and reviewer selection.
Native execution still uses the provider's permission system. Copies and retrospective
closeout checks do not constitute an OS security sandbox. The UI explains this and
also explains that direct edits can reach real files before checks finish.

Packaged workflow testing also exposed a closeout loop caused by the revision
recorded when authorizing test commands. Closeout now accepts that command-owned
revision while still checking the exact files and full request. After the judge
approves an unchanged submission, publication reuses its bound test evidence;
changed files, instructions, verification settings or access decisions invalidate it.

New goals use these versioned contracts. Existing goals preserve their saved execution
contract; they are not silently granted broader tools or workspace access. The complete
Python suite and installed-app release acceptance remain required release checks.

The unpublished v0.2.21 candidate failed fresh Windows installation twice with
`0xC0000005`. Its Electron Builder 25 dependency contained the known unsafe
per-user known-folder copy. Version 0.2.22 pins Electron Builder 26.15.3, which
contains the upstream bounded Unicode copy and preserves redirected per-user
installation folders. The installer checks remain mandatory; failed candidates
are not published or replaced in place.

The v0.2.22 app passed all installed-app checks and was published, but the public
source-ZIP bootstrap rejected its new application description after installation.
Version 0.2.23 recognizes both exact product-owned application descriptions while
keeping installer descriptions, publisher/product identity and versions strict.
Real Windows executable tests cover old/new metadata and rejected lookalikes;
the offline installation/desktop-redirection test now uses the new app metadata.
