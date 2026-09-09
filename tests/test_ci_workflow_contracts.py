"""CI must account for every expanded job before it can certify a run.

These source-only checks need no workflow/YAML package, shell, or runner APIs.
The readers intentionally accept only the scalar fields and inline matrix lists
used here, and fail if a workflow grows a shape they cannot verify.
"""

from __future__ import annotations

import itertools
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def block(source: str, key: str, indent: int) -> str:
    matches = list(re.finditer(rf"(?m)^{' ' * indent}{re.escape(key)}:[^\n]*$", source))
    if len(matches) != 1:
        raise AssertionError(f"Expected exactly one {key} field at indentation {indent}")
    match = matches[0]
    following = source[match.end():].lstrip("\n").splitlines()
    lines = []
    for line in following:
        if line.strip() and not line.lstrip().startswith("#"):
            if len(line) - len(line.lstrip()) <= indent:
                break
        lines.append(line)
    return "\n".join(lines)


def scalar(source: str, key: str, indent: int = 4) -> str:
    found = re.findall(rf"(?m)^{' ' * indent}{re.escape(key)}:\s*([^\n]+)$", source)
    if len(found) != 1:
        raise AssertionError(f"Expected exactly one scalar {key}")
    value = found[0].strip()
    if value.startswith(('"', "'")) and value[-1:] == value[:1]:
        value = value[1:-1]
    return value


def inline_list(value: str) -> list[str]:
    if not value.startswith("[") or not value.endswith("]"):
        raise AssertionError(f"Expected a concrete inline list, got {value!r}")
    items = [item.strip().strip("\"'") for item in value[1:-1].split(",")]
    if not items or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", item) for item in items):
        raise AssertionError(f"Unsupported or empty list: {value!r}")
    if len(items) != len(set(items)):
        raise AssertionError(f"Duplicate list values: {value!r}")
    return items


def jobs_in(source: str) -> dict[str, str]:
    content = block(source, "jobs", 0)
    starts = list(re.finditer(r"(?m)^  ([a-zA-Z0-9_-]+):\s*$", content))
    if not starts:
        raise AssertionError("No workflow jobs were found")
    jobs = {}
    for index, start in enumerate(starts):
        name = start[1]
        if name in jobs:
            raise AssertionError(f"Duplicate job {name}")
        end = starts[index + 1].start() if index + 1 < len(starts) else len(content)
        jobs[name] = content[start.end():end]
    return jobs


def matrix_axes(job: str) -> dict[str, list[str]]:
    if not re.search(r"(?m)^      matrix:", job):
        return {}
    matrix = block(job, "matrix", 6)
    axes = {}
    for line in matrix.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.fullmatch(r"        ([a-zA-Z0-9_-]+):\s*(\[.*\])\s*", line)
        if not match or match[1] in {"include", "exclude"} or match[1] in axes:
            raise AssertionError(f"Unsupported matrix definition: {line!r}")
        axes[match[1]] = inline_list(match[2])
    if not axes:
        raise AssertionError("Empty matrix")
    return axes


def expanded_names(jobs: dict[str, str]) -> list[str]:
    names = []
    for job in jobs.values():
        template = scalar(job, "name")
        axes = matrix_axes(job)
        for values in itertools.product(*axes.values()):
            assigned = dict(zip(axes, values))
            name = re.sub(r"\$\{\{\s*matrix\.([a-zA-Z0-9_-]+)\s*\}\}",
                          lambda match: assigned[match[1]], template)
            if "${{" in name:
                raise AssertionError(f"Unresolved job name: {name}")
            names.append(name)
    if len(names) != len(set(names)):
        raise AssertionError("Expanded job names must be unique for deadline evidence")
    return names


def guard_arguments(job: str, *, tag: bool) -> tuple[list[str], list[str]]:
    """Read the tag/manual manifest, rejecting shell branches we cannot decide."""
    script = block(job, "run", 8)
    active, branches, selected = True, [], []
    for raw in script.splitlines():
        line = raw.strip()
        if line.startswith("if "):
            match = re.fullmatch(r'if \[\[ "\$GITHUB_REF" (==|!=) refs/tags/v\* \]\]; then', line)
            if not match:
                raise AssertionError(f"Unsupported guard condition: {line}")
            condition = tag if match[1] == "==" else not tag
            branches.append((active, condition, False))
            active = active and condition
        elif line == "else":
            if not branches or branches[-1][2]:
                raise AssertionError("Unexpected guard else")
            parent, condition, _ = branches[-1]
            branches[-1] = (parent, condition, True)
            active = parent and not condition
        elif line == "fi":
            if not branches:
                raise AssertionError("Unexpected guard fi")
            active = branches.pop()[0]
        elif active and not line.startswith("#"):
            selected.append(line)
    if branches:
        raise AssertionError("Unclosed guard condition")
    flags = re.findall(r'--(required|allow-skipped)-job\s+"([^"\n]+)"', "\n".join(selected))
    return ([name for flag, name in flags if flag == "required"],
            [name for flag, name in flags if flag == "allow-skipped"])


def dependencies(job: str) -> list[str]:
    if not re.search(r"(?m)^    needs:", job):
        return []
    value = scalar(job, "needs")
    return inline_list(value) if value.startswith("[") else [value]


def steps_in(job: str) -> list[str]:
    starts = list(re.finditer(r"(?m)^      - (?:name|uses):", job))
    if not starts:
        raise AssertionError("No workflow steps were found")
    steps = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(job)
        # Normalize the list item's first field to the other step fields' indent.
        steps.append("        " + job[start.start() + 8:end])
    return steps


class WorkflowCoverageContractsTests(unittest.TestCase):
    def test_desktop_browser_tests_run_after_the_exact_runtime_is_built(self):
        desktop = jobs_in((WORKFLOWS / "checks.yml").read_text(encoding="utf-8"))["desktop"]
        self.assertLess(desktop.index("npm run build -- --win dir"), desktop.index("run: npm test"))
        self.assertIn('NEXUS_REQUIRE_BROWSER_RUNTIME_TESTS: "1"', desktop)
        self.assertIn('test_ordinary_local_suite_fixtures_loops_keyboard_modules_and_selection', desktop)

    def load(self, filename: str) -> dict[str, str]:
        return jobs_in((WORKFLOWS / filename).read_text(encoding="utf-8"))

    def assert_guard_covers(self, jobs, *, tag, count, allowed_skips=()):
        guards = [job for job in jobs.values() if scalar(job, "name") == "Workflow deadline"]
        self.assertEqual(len(guards), 1)
        required, skipped = guard_arguments(guards[0], tag=tag)
        expected = [name for name in expanded_names(jobs) if name != "Workflow deadline"]
        self.assertEqual(len(expected), count)
        self.assertCountEqual(required, expected)
        self.assertEqual(len(required), len(set(required)))
        self.assertCountEqual(skipped, allowed_skips)
        self.assertTrue(set(skipped) <= set(required), "Skip exceptions must still name required jobs")

    def test_checks_guard_matches_all_twelve_work_jobs(self):
        self.assert_guard_covers(self.load("checks.yml"), tag=False, count=12)

    def test_checks_removes_the_extra_panel_job_and_keeps_owning_acceptance(self):
        jobs = self.load("checks.yml")
        self.assertNotIn("panel", jobs)
        self.assertNotIn("Panel checks", expanded_names(jobs))
        self.assertEqual(scalar(jobs["project-checks"], "name"), "The project's own suite")
        self.assertEqual(scalar(jobs["desktop"], "name"), "Desktop app")
        self.assertEqual(scalar(jobs["package"], "name"), "What we would hand out")
        for owner in ("project-checks", "desktop", "package"):
            self.assertNotIn("continue-on-error:", jobs[owner])
        self.assertIn("python -m our_harness qa run --workers 4", jobs["project-checks"])
        self.assertIn("npm run smoke:browser-long-horizon", jobs["desktop"])
        self.assertIn("python scripts/verify_dist.py", jobs["package"])

    def test_checks_runs_the_complete_python313_suite_in_exactly_eight_parts(self):
        jobs = self.load("checks.yml")
        full_suite_jobs = [(owner, job) for owner, job in jobs.items()
                           if "python scripts/run_tests.py" in job]
        self.assertEqual([owner for owner, _ in full_suite_jobs], ["tests"])
        full_suite = full_suite_jobs[0][1]
        self.assertEqual(scalar(full_suite, "python-version", 10), "3.13")
        self.assertEqual(matrix_axes(full_suite), {"part": [str(part) for part in range(1, 9)]})
        self.assertEqual(full_suite.count("python scripts/run_tests.py --part ${{ matrix.part }}/8 --quiet"), 1)
        self.assertNotIn("continue-on-error:", full_suite)

    def test_python311_compatibility_is_one_bounded_explicit_contract_run(self):
        jobs = self.load("checks.yml")
        compatibility = jobs["python-compatibility"]
        self.assertEqual(scalar(compatibility, "name"), "Python 3.11 compatibility")
        self.assertEqual(scalar(compatibility, "python-version", 10), "3.11")
        self.assertEqual(scalar(compatibility, "timeout-minutes"), "3")
        self.assertEqual(matrix_axes(compatibility), {})
        self.assertEqual(dependencies(compatibility), [])
        self.assertNotIn("continue-on-error:", compatibility)
        self.assertNotIn("scripts/run_tests.py", compatibility)
        self.assertIn("python -m pip install -e .", compatibility)
        self.assertIn("python -m compileall -q src scripts", compatibility)
        self.assertIn("import our_harness.long_horizon", compatibility)
        self.assertIn("from our_harness.providers import codex_cli, subscription_cli", compatibility)
        focused = [step for step in steps_in(compatibility) if "python -m unittest" in step]
        self.assertEqual(len(focused), 1)
        command = " ".join(line.strip() for line in block(focused[0], "run", 8).splitlines())
        self.assertEqual(command.split(), [
            "python", "-m", "unittest", "tests.test_native_input_context",
            "tests.test_provider_connections", "tests.test_ci_budget",
            "tests.test_ci_workflow_contracts", "-q",
        ])
        self.assertCountEqual(
            [owner for owner, job in jobs.items() if 'python-version: "3.11"' in job],
            ["python-compatibility"],
        )

    def test_release_tag_requires_all_eight_jobs_to_succeed(self):
        self.assert_guard_covers(self.load("windows-release.yml"), tag=True, count=8)

    def test_manual_release_keeps_all_eight_jobs_with_only_publication_skips(self):
        self.assert_guard_covers(self.load("windows-release.yml"), tag=False, count=8,
                                 allowed_skips=("Publish release", "Public downloads", "Public source ZIP"))

    def test_every_job_and_explicit_step_cap_is_within_fifteen_minutes(self):
        for path in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
            for owner, job in jobs_in(path.read_text(encoding="utf-8")).items():
                with self.subTest(workflow=path.name, job=owner):
                    self.assertIn(int(scalar(job, "timeout-minutes")), range(1, 16))
                    for cap in re.findall(r"(?m)^\s+timeout-minutes:\s*([^\n]+)", job):
                        self.assertIn(int(cap), range(1, 16))

    def test_installed_modes_all_join_before_publication(self):
        jobs = self.load("windows-release.yml")
        acceptance = jobs["installed-acceptance"]
        self.assertEqual(matrix_axes(acceptance), {"mode": ["app", "long-horizon", "team-chat", "shortcuts"]})
        self.assertEqual(dependencies(acceptance), ["clean-windows-build"])
        self.assertCountEqual(dependencies(jobs["publish-release"]),
                              ["clean-windows-build", "installed-acceptance"])
        self.assertNotIn("continue-on-error:", acceptance)
        for owner in ("public-downloads", "prove-public-source-zip"):
            self.assertEqual(dependencies(jobs[owner]), ["publish-release"])

    def test_offline_bundle_runs_once_after_shortcut_acceptance_for_tag_refs(self):
        jobs = self.load("windows-release.yml")
        build = jobs["clean-windows-build"]
        acceptance = jobs["installed-acceptance"]
        self.assertNotIn("build_windows_offline_bundle.ps1", build)
        self.assertNotIn("nexus-harness-offline-", build)
        steps = steps_in(acceptance)
        verify = [step for step in steps if "name: Verify the installed artifact\n" in step]
        assemble = [step for step in steps if "build_windows_offline_bundle.ps1" in step]
        upload = [step for step in steps if "name: nexus-harness-offline-" in step]
        self.assertEqual((len(verify), len(assemble), len(upload)), (1, 1, 1))
        self.assertLess(steps.index(verify[0]), steps.index(assemble[0]))
        self.assertLess(steps.index(assemble[0]), steps.index(upload[0]))
        # A tag selected through workflow_dispatch must retain the same bundle
        # behavior as a pushed tag; branch dispatches still omit the bundle.
        condition = "matrix.mode == 'shortcuts' && startsWith(github.ref, 'refs/tags/v')"
        for step in (assemble[0], upload[0]):
            self.assertEqual(scalar(step, "if", 8), condition)
            self.assertNotIn("continue-on-error:", step)
        self.assertNotRegex(verify[0], r"(?m)^        if:")
        self.assertNotIn("continue-on-error:", acceptance)
        self.assertIn("Get-ChildItem -LiteralPath release-candidate", assemble[0])
        self.assertIn("if ($installers.Count -ne 1)", assemble[0])
        self.assertIn("-InstallerPath $installers[0].FullName", assemble[0])
        self.assertIn("-OutputDirectory (Resolve-Path 'release-candidate')", assemble[0])
        self.assertEqual(scalar(upload[0], "name", 10),
                         "nexus-harness-offline-${{ github.ref_name }}")
        self.assertEqual(scalar(upload[0], "path", 10),
                         "release-candidate/Nexus-Harness-Windows-Offline-*.zip")
        self.assertEqual(scalar(upload[0], "compression-level", 10), "0")
        self.assertEqual(scalar(upload[0], "if-no-files-found", 10), "error")

    def test_installer_artifact_still_serves_manual_and_tag_acceptance(self):
        jobs = self.load("windows-release.yml")
        build = jobs["clean-windows-build"]
        acceptance = jobs["installed-acceptance"]
        for job in (build, acceptance):
            self.assertNotRegex(job, r"(?m)^    if:")
        uploads = [step for step in steps_in(build) if "uses: actions/upload-artifact@" in step]
        self.assertEqual(len(uploads), 1)
        installer = uploads[0]
        self.assertNotRegex(installer, r"(?m)^        if:")
        self.assertEqual(scalar(installer, "name", 10),
                         "nexus-harness-windows-${{ github.ref_name }}")
        self.assertEqual([line.strip() for line in block(installer, "path", 10).splitlines()], [
            "desktop/build-output/Nexus-Harness-Setup-*.exe",
            "desktop/build-output/Nexus-Harness-Setup-*.exe.sha256",
            "desktop/build-output/release-metadata.json",
        ])
        self.assertEqual(scalar(installer, "compression-level", 10), "0")
        self.assertEqual(scalar(installer, "if-no-files-found", 10), "error")
        downloads = [step for step in steps_in(acceptance)
                     if "uses: actions/download-artifact@" in step]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(scalar(downloads[0], "name", 10), scalar(installer, "name", 10))
        self.assertEqual(scalar(downloads[0], "path", 10), "release-candidate")
        self.assertNotRegex(downloads[0], r"(?m)^        if:")
        publisher_download = [step for step in steps_in(jobs["publish-release"])
                              if "uses: actions/download-artifact@" in step]
        self.assertEqual(len(publisher_download), 1)
        self.assertEqual(scalar(publisher_download[0], "pattern", 10),
                         "nexus-harness-*-${{ github.ref_name }}")
        self.assertEqual(scalar(publisher_download[0], "merge-multiple", 10), "true")

    def test_publication_reserve_covers_write_and_longest_downstream_job(self):
        jobs = self.load("windows-release.yml")
        publisher = jobs["publish-release"]
        reserves = re.findall(r"ci_budget\.py check --reserve-seconds ([0-9]+)", publisher)
        self.assertEqual(len(reserves), 1)
        publish_caps = re.findall(r"(?m)^        timeout-minutes:\s*([0-9]+)\s*$", publisher)
        self.assertEqual(len(publish_caps), 1, "The externally visible write needs its own time cap")
        downstream_caps = [int(scalar(job, "timeout-minutes")) for job in jobs.values()
                           if "publish-release" in dependencies(job)]
        self.assertTrue(downstream_caps)
        self.assertGreaterEqual(int(reserves[0]), 60 * (int(publish_caps[0]) + max(downstream_caps)))

    def test_skip_permission_without_required_job_reproduces_the_rejected_manual_manifest(self):
        jobs = self.load("windows-release.yml")
        guard_owner = next(owner for owner, job in jobs.items()
                           if scalar(job, "name") == "Workflow deadline")
        broken = dict(jobs)
        broken[guard_owner] = jobs[guard_owner].replace('--required-job "Publish release"', "")
        with self.assertRaises(AssertionError):
            self.assert_guard_covers(broken, tag=False, count=8,
                                     allowed_skips=("Publish release", "Public downloads", "Public source ZIP"))

    def test_readers_reject_unverifiable_matrix_and_guard_shapes(self):
        with self.assertRaises(AssertionError):
            matrix_axes("      matrix:\n        include:\n          - mode: app\n")
        with self.assertRaises(AssertionError):
            guard_arguments('        run: |\n          if arbitrary-command; then\n          fi\n', tag=True)


if __name__ == "__main__":
    unittest.main()
