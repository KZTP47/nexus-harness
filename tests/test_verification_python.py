"""Fresh Windows source checkouts get a small, pinned verification Python."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from our_harness import swarm_work
from our_harness import verification_python as runtime
from our_harness.config import DEFAULT_CONFIG, LoadedConfig


def embedded_archive(*, unsafe_name: str = "") -> bytes:
    files = {
        "python.exe": b"exe",
        "python3.dll": b"abi",
        "python311.dll": b"runtime",
        "python311.zip": b"stdlib",
        "python311._pth": b"python311.zip\n.\n",
        "vcruntime140.dll": b"vcruntime",
        "vcruntime140_1.dll": b"vcruntime1",
        "_socket.pyd": b"socket",
    }
    if unsafe_name:
        files[unsafe_name] = b"escape"
    held = io.BytesIO()
    with zipfile.ZipFile(held, "w", compression=zipfile.ZIP_DEFLATED) as packed:
        for name, payload in files.items():
            packed.writestr(name, payload)
    return held.getvalue()


class LightweightVerificationPythonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cache = self.root / "private-cache"
        self.snapshot = self.root / "snapshot"
        self.snapshot.mkdir()
        self.guard = self.snapshot / ".nexus-verification"
        self.guard.mkdir()

    def release(self, raw: bytes):
        return mock.patch.object(runtime, "PYTHON_SHA256", hashlib.sha256(raw).hexdigest())

    def test_packaged_runtime_is_preferred_only_when_its_core_and_manifest_agree(self) -> None:
        packaged = self.root / "packaged"
        packaged.mkdir()
        for name in (
            "python.exe", "python3.dll", "python311.dll", "python311.zip",
            "python311._pth", "vcruntime140.dll", "vcruntime140_1.dll",
        ):
            (packaged / name).write_bytes(name.encode("ascii"))
        (packaged / "NEXUS_RUNTIME.json").write_text(json.dumps({
            "python": runtime.PYTHON_VERSION,
            "python_sha256": runtime.PYTHON_SHA256,
        }), encoding="utf-8")
        self.assertEqual(packaged, runtime.packaged_runtime_if_usable(packaged))
        (packaged / "python311.zip").unlink()
        self.assertIsNone(runtime.packaged_runtime_if_usable(packaged))

    def test_verified_cache_hit_never_reaches_the_network(self) -> None:
        raw = embedded_archive()
        with self.release(raw):
            archive = runtime._cache_archive(self.cache)
            archive.parent.mkdir(parents=True)
            archive.write_bytes(raw)
            with mock.patch.object(
                runtime, "_download_official_archive",
                side_effect=AssertionError("network should not be used"),
            ):
                self.assertEqual(raw, runtime._verified_archive_bytes(self.cache))

    def test_wrong_download_checksum_fails_closed_without_publishing_it(self) -> None:
        raw = embedded_archive()
        with mock.patch.object(runtime, "PYTHON_SHA256", "0" * 64), \
                mock.patch.object(runtime, "_download_official_archive", return_value=raw):
            with self.assertRaisesRegex(runtime.VerificationPythonUnavailable, "checksum"):
                runtime._verified_archive_bytes(self.cache)
            self.assertFalse(list(self.cache.glob("*.zip")))
            self.assertFalse(list(self.cache.glob("*.part")))

    def test_unsafe_archive_member_is_rejected_before_any_extraction(self) -> None:
        raw = embedded_archive(unsafe_name="../outside.txt")
        with self.release(raw), mock.patch.object(
            runtime, "_download_official_archive", return_value=raw,
        ):
            with self.assertRaisesRegex(runtime.VerificationPythonUnavailable, "unsafe member"):
                runtime.stage_source_runtime(
                    self.guard / "runtime", snapshot=self.snapshot,
                    python_guard_parent=self.guard, cache_root=self.cache,
                )
        self.assertFalse((self.root / "outside.txt").exists())
        self.assertFalse((self.guard / "runtime").exists())

    def test_parallel_cache_users_publish_one_complete_archive(self) -> None:
        raw = embedded_archive()
        calls = 0

        def download() -> bytes:
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return raw

        with self.release(raw), mock.patch.object(
            runtime, "_download_official_archive", side_effect=download,
        ):
            with ThreadPoolExecutor(max_workers=8) as workers:
                results = list(workers.map(
                    lambda _one: runtime._verified_archive_bytes(self.cache), range(8),
                ))
            self.assertEqual([raw] * 8, results)
            self.assertEqual(1, calls)
            self.assertEqual(raw, runtime._cache_archive(self.cache).read_bytes())
            self.assertFalse(list(self.cache.glob("*.part")))

    def test_lock_timeout_does_not_try_to_unlock_a_lock_never_acquired(self) -> None:
        if os.name == "nt":
            import msvcrt

            target = "msvcrt.locking"
            original = msvcrt.locking
        else:
            import fcntl

            target = "fcntl.flock"
            original = fcntl.flock
        calls = 0

        def unavailable(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise OSError("held")

        with mock.patch(target, side_effect=unavailable):
            with self.assertRaisesRegex(
                runtime.VerificationPythonUnavailable, "timed out waiting",
            ):
                with runtime._file_lock(self.cache / "busy.lock", timeout=0):
                    self.fail("an unavailable lock must not be entered")
        self.assertEqual(1, calls)
        self.assertTrue(callable(original))

    def test_parallel_snapshot_staging_is_atomic_and_complete(self) -> None:
        raw = embedded_archive()
        destination = self.guard / "runtime"
        with self.release(raw), mock.patch.object(
            runtime, "_download_official_archive", return_value=raw,
        ):
            with ThreadPoolExecutor(max_workers=6) as workers:
                results = list(workers.map(
                    lambda _one: runtime.stage_source_runtime(
                        destination, snapshot=self.snapshot,
                        python_guard_parent=self.guard, cache_root=self.cache,
                    ),
                    range(6),
                ))
        self.assertEqual([destination] * 6, results)
        self.assertEqual(b"exe", (destination / "python.exe").read_bytes())
        pth = (destination / "python311._pth").read_text(encoding="utf-8")
        self.assertIn(str(self.snapshot), pth)
        self.assertIn(str(self.guard), pth)
        self.assertFalse(list(self.guard.glob(".runtime-stage-*")))
        self.assertFalse(list(self.guard.glob(".runtime-previous-*")))

    def test_guard_and_dependency_paths_must_belong_to_the_exact_snapshot(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        with mock.patch.object(
            runtime, "_verified_archive_bytes",
            side_effect=AssertionError("validation must precede cache or network access"),
        ):
            with self.assertRaisesRegex(runtime.VerificationPythonUnavailable, "guard"):
                runtime.stage_source_runtime(
                    self.guard / "runtime", snapshot=self.snapshot,
                    python_guard_parent=outside, cache_root=self.cache,
                )
            with self.assertRaisesRegex(runtime.VerificationPythonUnavailable, "dependency"):
                runtime.stage_source_runtime(
                    self.guard / "runtime", snapshot=self.snapshot,
                    python_guard_parent=self.guard, cache_root=self.cache,
                    dependency_paths=(outside,),
                )

    def test_runtime_file_collision_is_replaced_and_abandoned_artifacts_are_removed(self) -> None:
        raw = embedded_archive()
        destination = self.guard / "runtime"
        destination.write_bytes(b"project supplied executable placeholder")
        (self.guard / ".runtime-stage-abandoned").write_bytes(b"stale")
        previous = self.guard / ".runtime-previous-abandoned"
        previous.mkdir()
        (previous / "old.txt").write_text("old", encoding="utf-8")
        with self.release(raw), mock.patch.object(
            runtime, "_download_official_archive", return_value=raw,
        ):
            result = runtime.stage_source_runtime(
                destination, snapshot=self.snapshot,
                python_guard_parent=self.guard, cache_root=self.cache,
            )
        self.assertEqual(destination, result)
        self.assertTrue(destination.is_dir())
        self.assertEqual(b"exe", (destination / "python.exe").read_bytes())
        self.assertFalse(list(self.guard.glob(".runtime-stage-*")))
        self.assertFalse(list(self.guard.glob(".runtime-previous-*")))

    def test_lock_directory_collision_is_a_controlled_unavailable_result(self) -> None:
        raw = embedded_archive()
        lock = self.guard / ".source-python-stage.lock"
        lock.mkdir()
        with self.release(raw), mock.patch.object(
            runtime, "_download_official_archive", return_value=raw,
        ):
            with self.assertRaisesRegex(
                runtime.VerificationPythonUnavailable, "lock file could not be opened",
            ):
                runtime.stage_source_runtime(
                    self.guard / "runtime", snapshot=self.snapshot,
                    python_guard_parent=self.guard, cache_root=self.cache,
                )
        self.assertFalse((self.guard / "runtime").exists())

    def test_conventional_project_dependencies_are_explicitly_added_without_installing(self) -> None:
        dependency = self.snapshot / ".venv" / "Lib" / "site-packages"
        dependency.mkdir(parents=True)
        (dependency / "already_prepared.py").write_text("VALUE = 7\n", encoding="utf-8")
        paths = runtime.snapshot_dependency_paths(self.snapshot)
        self.assertEqual((dependency.resolve(),), paths)
        raw = embedded_archive()
        with self.release(raw), mock.patch.object(
            runtime, "_download_official_archive", return_value=raw,
        ):
            runtime.stage_source_runtime(
                self.guard / "runtime", snapshot=self.snapshot,
                python_guard_parent=self.guard, cache_root=self.cache,
                dependency_paths=paths,
            )
        pth = (self.guard / "runtime" / "python311._pth").read_text(encoding="utf-8")
        self.assertIn(str(dependency.resolve()), pth)

    def test_snapshot_copy_reserves_verification_namespace(self) -> None:
        project = self.root / "project"
        copied = self.root / "copied"
        project.mkdir()
        (project / "kept.py").write_text("KEPT = True\n", encoding="utf-8")
        supplied = project / ".nexus-verification"
        supplied.mkdir()
        (supplied / "runtime").write_bytes(b"attacker runtime")
        (supplied / "sitecustomize.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
        swarm_work._copy_verification_snapshot(project, copied)
        self.assertTrue((copied / "kept.py").is_file())
        self.assertEqual([], list((copied / ".nexus-verification").iterdir()))

    def test_packaged_python_runtime_replaces_a_preseeded_runtime_and_is_verified(self) -> None:
        bundled = self.root / "packaged-runtime"
        bundled.mkdir()
        for name in (
            "python.exe", "python3.dll", "python311.dll",
            "vcruntime140.dll", "vcruntime140_1.dll", "python311.zip",
        ):
            (bundled / name).write_bytes(("owned-" + name).encode("ascii"))
        (bundled / "Lib" / "site-packages").mkdir(parents=True)
        destination = self.guard / "runtime"
        destination.write_bytes(b"project supplied runtime")
        dependency = self.snapshot / "vendor"
        dependency.mkdir()
        result = swarm_work._stage_packaged_python_runtime(
            bundled, destination, snapshot=self.snapshot,
            python_guard_parent=self.guard,
            dependency_paths=(dependency,),
        )
        self.assertEqual(destination, result)
        self.assertTrue(destination.is_dir())
        self.assertEqual(b"owned-python.exe", (destination / "python.exe").read_bytes())
        pth = (destination / "python311._pth").read_text(encoding="utf-8")
        self.assertIn(str(dependency.resolve()), pth)
        self.assertFalse(list(self.guard.glob(".runtime-previous-*")))

    @unittest.skipUnless(os.name == "nt", "Windows junction cleanup regression")
    def test_engine_namespace_junction_is_unlinked_without_touching_its_target(self) -> None:
        target = self.root / "junction-target"
        target.mkdir()
        sentinel = target / "must-survive.txt"
        sentinel.write_text("safe", encoding="utf-8")
        junction = self.snapshot / "junction"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            check=False, capture_output=True, text=True,
        )
        if created.returncode:
            self.skipTest("this Windows volume could not create a directory junction")
        swarm_work._recreate_verification_directory(junction)
        self.assertTrue(junction.is_dir())
        self.assertFalse(swarm_work._verification_is_reparse(junction))
        self.assertEqual("safe", sentinel.read_text(encoding="utf-8"))

    def test_project_owned_node_executable_is_rejected_before_it_can_run(self) -> None:
        supplied = self.snapshot / "node.exe"
        supplied.write_bytes(b"not a trusted host runtime")
        with self.assertRaisesRegex(
            runtime.VerificationPythonUnavailable, "project-controlled",
        ):
            swarm_work._trusted_host_node(
                str(supplied), snapshot=self.snapshot, denied_root=self.snapshot,
            )

    @unittest.skipUnless(os.name == "nt", "Windows containment failure boundary")
    def test_source_bootstrap_failure_runs_no_project_code(self) -> None:
        config = LoadedConfig(dict(DEFAULT_CONFIG), self.root, [], {})
        with mock.patch.object(swarm_work, "appcontainer_available", return_value=True), \
                mock.patch.object(
                    swarm_work, "_windows_containment_canary",
                    return_value={"passed": True},
                ), \
                mock.patch.object(
                    swarm_work, "packaged_runtime_if_usable", return_value=None,
                ), \
                mock.patch.object(
                    swarm_work, "stage_source_runtime",
                    side_effect=runtime.VerificationPythonUnavailable("offline"),
                ), \
                mock.patch.object(swarm_work, "run_appcontainer") as launch:
            result = swarm_work._contained_snapshot_command(
                config, self.snapshot, ["python", "-c", "raise SystemExit('ran')"],
                timeout=10, denied_root=self.root,
            )
        self.assertEqual(-2, result["exit_code"])
        self.assertTrue(result["containment_unavailable"])
        self.assertIn("Nexus did not run project code", result["stderr"])
        launch.assert_not_called()

    @unittest.skipUnless(os.name == "nt" and shutil.which("node"), "Windows Node runtime probe")
    def test_node_runtime_collision_is_replaced_before_contained_launch(self) -> None:
        config = LoadedConfig(dict(DEFAULT_CONFIG), self.root, [], {})
        runtime_root = self.guard / "runtime"
        runtime_root.mkdir()
        (runtime_root / "node.exe").write_bytes(b"project supplied node")
        result = swarm_work._contained_snapshot_command(
            config, self.snapshot,
            [shutil.which("node") or "node", "-e", "process.stdout.write('contained node')"],
            timeout=20, denied_root=self.root,
        )
        self.assertEqual(0, result["exit_code"], result)
        self.assertIn("contained node", result["stdout"])
        copied = self.guard / "runtime" / "node.exe"
        self.assertEqual(
            swarm_work.file_sha256(Path(shutil.which("node") or "node")),
            swarm_work.file_sha256(copied),
        )

    @unittest.skipUnless(os.name == "nt", "Windows contained dependency probe")
    def test_bare_pytest_uses_prepared_snapshot_dependency_without_installing(self) -> None:
        config = LoadedConfig(dict(DEFAULT_CONFIG), self.root, [], {})
        packages = self.snapshot / ".venv" / "Lib" / "site-packages"
        pytest_package = packages / "pytest"
        dependency_package = packages / "prepared_dependency"
        pytest_package.mkdir(parents=True)
        dependency_package.mkdir()
        (dependency_package / "__init__.py").write_text("VALUE = 'dependency worked'\n", encoding="utf-8")
        (pytest_package / "__init__.py").write_text("", encoding="utf-8")
        (pytest_package / "__main__.py").write_text(
            "import os\nfrom pathlib import Path\nfrom prepared_dependency import VALUE\n"
            "Path(os.environ['NEXUS_VERIFICATION_ROOT'], 'pytest-dependency-ok.txt').write_text(VALUE, encoding='utf-8')\n",
            encoding="utf-8",
        )
        with mock.patch.object(
            swarm_work, "packaged_runtime_if_usable", return_value=None,
        ), mock.patch.object(
            runtime, "_download_official_archive",
            side_effect=AssertionError("the already verified cache should satisfy this probe"),
        ):
            result = swarm_work._contained_snapshot_command(
                config, self.snapshot, ["pytest"], timeout=30, denied_root=self.root,
            )
        self.assertEqual(0, result["exit_code"], result)
        self.assertEqual(
            "dependency worked",
            (self.snapshot / "pytest-dependency-ok.txt").read_text(encoding="utf-8"),
        )
        self.assertEqual("python.exe", Path(result["contained_argv"][0]).name.casefold())
        self.assertEqual(["-m", "pytest"], result["contained_argv"][1:])

    @unittest.skipUnless(os.name == "nt", "Windows missing-dependency guidance probe")
    def test_missing_pytest_reports_the_explicit_no_install_strategy(self) -> None:
        config = LoadedConfig(dict(DEFAULT_CONFIG), self.root, [], {})
        with mock.patch.object(
            swarm_work, "packaged_runtime_if_usable", return_value=None,
        ):
            result = swarm_work._contained_snapshot_command(
                config, self.snapshot, ["pytest", "-q"],
                timeout=20, denied_root=self.root,
            )
        self.assertNotEqual(0, result["exit_code"], result)
        self.assertIn(".venv/Lib/site-packages", result["stderr"])
        self.assertIn("does not install packages silently", result["stderr"])

    @unittest.skipUnless(os.name == "nt", "Windows source-runtime AppContainer probe")
    def test_fresh_source_runtime_runs_inside_the_real_appcontainer(self) -> None:
        config = LoadedConfig(dict(DEFAULT_CONFIG), self.root, [], {})
        sibling = self.root / "outside-snapshot.txt"
        local = self.snapshot / "inside-snapshot.txt"
        child_source = (
            "from pathlib import Path;"
            f"Path({str(sibling)!r}).write_text('escape',encoding='utf-8')"
        )
        source = (
            "import ctypes,pathlib,site,socket,subprocess,sys;"
            f"child=subprocess.run([sys.executable,'-c',{child_source!r}]);"
            "assert child.returncode != 0;"
            "server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1);"
            "client=socket.socket();client.connect(server.getsockname());"
            "accepted,_=server.accept();client.sendall(b'ok');"
            "assert accepted.recv(2)==b'ok';accepted.close();client.close();server.close();"
            "pathlib.Path('inside-snapshot.txt').write_text("
            "site.__file__+'|'+str(ctypes.windll.kernel32.GetCurrentProcessId()),encoding='utf-8')"
        )
        with mock.patch.object(
            swarm_work, "packaged_runtime_if_usable", return_value=None,
        ):
            result = swarm_work._contained_snapshot_command(
                config, self.snapshot, ["python", "-c", source],
                timeout=30, denied_root=self.root,
            )
        self.assertEqual(0, result["exit_code"], result)
        self.assertTrue(local.is_file(), result)
        self.assertIn("site.py", local.read_text(encoding="utf-8"), result)
        self.assertFalse(sibling.exists(), result)
        self.assertEqual("windows-appcontainer-job-v1", result["containment_profile"])
        self.assertTrue(result["containment_attestation"]["child_inherited_boundary"])


if __name__ == "__main__":
    unittest.main()
