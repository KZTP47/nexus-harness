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


class WorkflowCoverageContractsTests(unittest.TestCase):
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

    def test_checks_guard_matches_all_twenty_expanded_jobs(self):
        self.assert_guard_covers(self.load("checks.yml"), tag=False, count=20)

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
