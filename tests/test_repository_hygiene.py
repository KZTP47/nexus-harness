"""Release guards for files that belong to one developer or one machine."""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]


def git(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=check,
    )


class RepositoryHygieneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (ROOT / ".git").exists():
            raise unittest.SkipTest("repository-only hygiene checks need the Git index")

    def test_private_and_machine_local_files_are_not_tracked(self) -> None:
        tracked = {
            PurePosixPath(line.strip())
            for line in git("ls-files").stdout.splitlines()
            if line.strip()
        }
        forbidden_exact = {
            PurePosixPath(".env"),
            PurePosixPath(".npmrc"),
            PurePosixPath(".pypirc"),
            PurePosixPath(".harness/config.local.json"),
            PurePosixPath(".harness/project-authority.json"),
            PurePosixPath("harness-setup.json"),
            PurePosixPath("Nexus Harness.lnk"),
            # These former documentation assets were captured from a live
            # developer profile. Public screenshots must use new filenames
            # produced by a deterministic synthetic fixture.
            PurePosixPath("docs/images/agent-swarm-board.png"),
            PurePosixPath("docs/images/seats.png"),
            PurePosixPath("docs/images/talk-to-them.png"),
            PurePosixPath("docs/images/your-team.png"),
            PurePosixPath(".harness/qa/baselines/looks-the-same.png"),
        }
        forbidden_roots = (
            PurePosixPath(".claude"),
            PurePosixPath(".codex"),
            PurePosixPath(".idea"),
            PurePosixPath(".vscode"),
            PurePosixPath(".harness/chats"),
            PurePosixPath(".harness/memory"),
            PurePosixPath(".harness/pages"),
            PurePosixPath(".harness/runtime"),
            PurePosixPath(".harness/vault"),
            PurePosixPath("desktop/build-output"),
            PurePosixPath("desktop/runtime"),
        )
        private_suffixes = {
            ".key", ".lnk", ".p12", ".pfx", ".pem", ".sqlite", ".sqlite3",
        }

        forbidden = []
        for path in sorted(tracked):
            # During the cleanup commit Git still lists a tracked file until
            # its deletion is staged. In a committed checkout every indexed
            # path exists, so ignoring an already-removed working-tree entry
            # does not weaken the release guard.
            if not (ROOT / Path(path.as_posix())).exists():
                continue
            if path in forbidden_exact:
                forbidden.append(path.as_posix())
                continue
            if path.name.startswith(".env") and path.name != ".env.example":
                forbidden.append(path.as_posix())
                continue
            if path.suffix.casefold() in private_suffixes:
                forbidden.append(path.as_posix())
                continue
            if any(path == root or root in path.parents for root in forbidden_roots):
                forbidden.append(path.as_posix())

        self.assertEqual(forbidden, [])
        self.assertIn(PurePosixPath(".env.example"), tracked)

    def test_gitignore_protects_common_local_inputs(self) -> None:
        examples = (
            ".env",
            ".env.local",
            ".npmrc",
            ".pypirc",
            ".claude/settings.local.json",
            ".codex/config.toml",
            "developer-signing-key.pem",
            "developer-signing-key.pfx",
            "harness-setup.json",
        )
        # Ask Git about paths without creating them. One argv path per call
        # also avoids Windows pipe newline translation becoming part of the
        # candidate filename.
        missing = [
            path for path in examples
            if git("check-ignore", "--no-index", "--quiet", "--", path, check=False).returncode
            != 0
        ]
        self.assertEqual(missing, [])

    def test_shared_project_identity_is_the_product_name(self) -> None:
        metadata = json.loads(
            (ROOT / ".harness" / "project.json").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata, {"name": "Nexus Harness"})

    def test_known_live_configuration_identities_are_not_published(self) -> None:
        # Keep the live identity itself out of this repository too.
        forbidden = ("f65e10" + "dbbbab",)
        findings = []
        for raw in git("ls-files", "-z").stdout.split("\0"):
            if not raw:
                continue
            path = ROOT / Path(PurePosixPath(raw).as_posix())
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").casefold()
            if any(identity in text for identity in forbidden):
                findings.append(raw)
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
