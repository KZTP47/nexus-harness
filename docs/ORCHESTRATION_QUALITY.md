# Orchestration quality and verification

Nexus controls a different workflow from a provider's standalone coding app.
Provider identity and model settings matter, but so do the tools and evidence
available to the agent. A claim of equivalent output quality would require a
controlled comparison; an orchestration bug does not establish a model-quality
difference.

Project collaboration uses Nexus's bounded file/context tools and validated
file transactions. Its ephemeral CLI adapters do not inherit the standalone
app's full personal configuration and unrestricted native tool inventory.
Agents must know which tools actually exist and use the supported verification
path, rather than assume that reading source code amounts to running it.

An earlier shared-goal policy allowed a task without selected tests to complete
from current file evidence and agreement between its contributors. That was
insufficient for executable deliverables: an inert launch button could pass as
complete even though no browser interaction had been exercised. Version 0.2.10
requires executed checks for runtime deliverables and directs agents to author
meaningful missing checks before trying again. Repeated failures are bounded;
Nexus does not manufacture progress or silently approve new commands.

The verification policy is versioned and bound to saved goal context. Fresh and
incomplete resumed goals use the current policy while preserving the user's
exact command authority. Historical completed goals remain historical records;
the update does not retroactively claim to have tested them.

## Check behavior and the delivery method

A browser game's entry page, script imports, launch action and state changes
are separate requirements. For example, a relative ES-module script can fail
when the user double-clicks the HTML file. Browsers apply module origin rules
to `file:` URLs; the [MDN modules guide](https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide/Modules)
and [Three.js installation guide](https://threejs.org/manual/en/installation.html)
describe why normal development uses a local HTTP server. A deliverable must
either support the promised launch method or supply the correct launcher and
instructions, then test that path.

Nexus's bundled browser verifier supports a contained, deterministic Playwright
subset: a literal project route, click/fill actions, and native DOM assertions.
The provider context supplies an exact recipe. The selected test command still
passes through existing command approval, disposable-project execution and
containment checks. Browser checks over an HTTP route must never be relabelled
as evidence that direct `file:` launch works.

Tests should fail with the relevant production behavior broken and pass when it
works. Source inventory, syntax checks and copied expected values alone are not
proof of interactive behavior. The reviewer must inspect concrete failures,
launch conditions and the user's acceptance criteria; praise or agreement is
not an execution result.

The regression suite covers inert-versus-working launch handlers in the real
contained browser runner, missing verification, placeholder-only game output,
unsupported direct-file launch claims, bounded repair, and resumed contracts.
