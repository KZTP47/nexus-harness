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

from our_harness import swarm_work, windows_containment
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
        self.guard = self.root / "host-engine"
        self.guard.mkdir()

    def release(self, raw: bytes):
        return mock.patch.object(runtime, "PYTHON_SHA256", hashlib.sha256(raw).hexdigest())

    def test_partial_appcontainer_acl_grants_are_rolled_back(self) -> None:
        read_root = self.root / "read-runtime"
        read_root.mkdir()

        def completed(command: list[str]) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(command, 0, "", "")

        scenarios = ("snapshot", "ancestor", "read")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                commands: list[list[str]] = []
                failed = False

                def bounded(command: list[str], *, text: bool = False):
                    nonlocal failed
                    commands.append(command)
                    joined = " ".join(command)
                    should_fail = (
                        (scenario == "snapshot" and "/grant" in command and "(OI)(IO)" in joined)
                        or (scenario == "ancestor" and "(S,RA,X)" in joined)
                        or (scenario == "read" and "(OI)(CI)(RX)" in joined)
                    )
                    if should_fail and not failed:
                        failed = True
                        raise OSError("injected authority fault")
                    return completed(command)

                lease = windows_containment._AppContainerAuthorityLease(
                    self.snapshot, "S-1-15-2-123",
                    read_execute_roots=(read_root,) if scenario == "read" else (),
                    transient_read_execute_roots=(read_root,),
                    grant_traverse_ancestors=scenario == "ancestor",
                    map_authorized_roots=False,
                )
                with mock.patch.object(
                    windows_containment, "_bounded_command", side_effect=bounded,
                ):
                    with self.assertRaisesRegex(OSError, "injected authority fault"):
                        lease.prepare()
                self.assertTrue(failed)
                self.assertTrue(any("/remove:g" in command for command in commands), commands)
                if scenario == "snapshot":
                    self.assertTrue(any("/reset" in command for command in commands), commands)
                if scenario == "read":
                    self.assertFalse(any(
                        key[0] == "S-1-15-2-123"
                        for key in windows_containment._PERSISTENT_RX_GRANTS
                    ))

    def test_post_mapping_failure_uses_the_same_authority_rollback_owner(self) -> None:
        commands: list[list[str]] = []

        def bounded(command: list[str], *, text: bool = False):
            commands.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        lease = windows_containment._AppContainerAuthorityLease(
            self.snapshot, "S-1-15-2-456",
            read_execute_roots=(), transient_read_execute_roots=(),
            grant_traverse_ancestors=False, map_authorized_roots=True,
        )
        with mock.patch.object(
            windows_containment, "_bounded_command", side_effect=bounded,
        ), mock.patch.object(
            windows_containment, "_map_roots_to_private_drives",
            return_value=({str(self.snapshot): "Z:\\"}, 77),
        ), mock.patch.object(
            windows_containment, "_unmap_private_drives",
        ) as unmap:
            lease.prepare()
            # Simulate any computation/launch fault immediately after mapping.
            lease.cleanup(process_started=False)
        unmap.assert_called_once_with({str(self.snapshot): "Z:\\"}, 77)
        self.assertTrue(any("/remove:g" in command for command in commands), commands)

    def test_nonzero_acl_cleanup_is_reported_and_rx_lease_is_retained(self) -> None:
        read_root = self.root / "transient-runtime"
        read_root.mkdir()
        grant_key = ("S-1-15-2-789", str(read_root.resolve()).casefold())
        lease = windows_containment._AppContainerAuthorityLease(
            self.snapshot, grant_key[0], read_execute_roots=(read_root,),
            transient_read_execute_roots=(read_root,),
            grant_traverse_ancestors=False, map_authorized_roots=False,
        )
        lease.new_read_grants.append((grant_key, read_root.resolve()))
        windows_containment._PERSISTENT_RX_GRANTS.add(grant_key)
        self.addCleanup(windows_containment._PERSISTENT_RX_GRANTS.discard, grant_key)
        failed = subprocess.CompletedProcess(
            ["icacls"], 5, "", "Access is denied."
        )
        with mock.patch.object(
            windows_containment, "_bounded_command", return_value=failed,
        ):
            lease.cleanup(process_started=False)
        self.assertTrue(lease.cleanup_errors)
        self.assertIn("Access is denied", " | ".join(lease.cleanup_errors))
        # A failed removal must remain registered for the process-exit retry.
        self.assertIn(grant_key, windows_containment._PERSISTENT_RX_GRANTS)

    @unittest.skipUnless(os.name == "nt", "Windows mapping mutex cleanup")
    def test_drive_unmap_releases_mutex_even_when_subst_cleanup_times_out(self) -> None:
        kernel32 = mock.Mock()
        with mock.patch.object(
            windows_containment, "_bounded_command",
            side_effect=subprocess.TimeoutExpired(["subst"], 1),
        ), mock.patch.object(
            windows_containment.ctypes.windll, "kernel32", kernel32,
        ):
            with self.assertRaisesRegex(OSError, "could not be removed"):
                windows_containment._unmap_private_drives({"root": "Z:\\"}, 99)
        kernel32.ReleaseMutex.assert_called_once_with(99)
        kernel32.CloseHandle.assert_called_once_with(99)

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

    def test_guard_must_be_external_and_dependencies_must_belong_to_snapshot(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        snapshot_guard = self.snapshot / ".nexus-verification"
        snapshot_guard.mkdir()
        with mock.patch.object(
            runtime, "_verified_archive_bytes",
            side_effect=AssertionError("validation must precede cache or network access"),
        ):
            with self.assertRaisesRegex(runtime.VerificationPythonUnavailable, "guard"):
                runtime.stage_source_runtime(
                    snapshot_guard / "runtime", snapshot=self.snapshot,
                    python_guard_parent=snapshot_guard, cache_root=self.cache,
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
    def test_snapshot_node_runtime_collision_is_scrubbed_and_external_runtime_runs(self) -> None:
        config = LoadedConfig(dict(DEFAULT_CONFIG), self.root, [], {})
        runtime_root = self.snapshot / ".nexus-verification" / "runtime"
        runtime_root.mkdir(parents=True)
        (runtime_root / "node.exe").write_bytes(b"project supplied node")
        result = swarm_work._contained_snapshot_command(
            config, self.snapshot,
            [shutil.which("node") or "node", "-e", "process.stdout.write('contained node')"],
            timeout=20, denied_root=self.root,
        )
        self.assertEqual(0, result["exit_code"], result)
        self.assertIn("contained node", result["stdout"])
        contained = Path(result["contained_argv"][0])
        self.assertEqual("node.exe", contained.name.casefold())
        self.assertFalse(contained.is_relative_to(self.snapshot.resolve()))
        self.assertFalse(runtime_root.exists())

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
        # Make the cache precondition explicit and private to this test.  A
        # developer machine usually already has the pinned archive, which hid
        # the clean-runner dependency on mutable per-user state.
        runtime._verified_archive_bytes(self.cache)
        with mock.patch.object(
            swarm_work, "packaged_runtime_if_usable", return_value=None,
        ), mock.patch.object(
            runtime, "default_cache_root", return_value=self.cache,
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

    @unittest.skipUnless(os.name == "nt", "Windows contained reparse cleanup probe")
    def test_project_created_reparse_is_unlinked_before_bounded_acl_cleanup(self) -> None:
        target = self.root / "unrelated-reparse-target"
        target.mkdir()
        sentinel = target / "sentinel.txt"
        sentinel.write_text("unrelated target must survive", encoding="utf-8")
        link = self.snapshot / "project-created-junction"
        system_root = Path(os.environ.get("SystemRoot", "C:\\Windows"))
        environment = {
            key: os.environ[key] for key in (
                "SystemRoot", "WINDIR", "COMSPEC", "PATH", "PATHEXT",
                "SYSTEMDRIVE", "LOCALAPPDATA", "APPDATA", "USERNAME",
            ) if key in os.environ
        }
        result = windows_containment.run_appcontainer(
            self.snapshot,
            [
                str(system_root / "System32" / "cmd.exe"),
                "/d", "/s", "/c", "echo ok>ordinary-after-link.txt",
            ],
            environment, 20.0, reparse_probe=(link, target),
        )
        self.assertEqual(0, result["exit_code"], result)
        self.assertFalse(result["containment_cleanup_error"], result)
        self.assertTrue(result["reparse_created"], result)
        self.assertIn(
            "project-created-junction",
            result["cleanup_reparse_entries_removed"],
        )
        self.assertFalse(link.exists())
        self.assertEqual(
            "unrelated target must survive", sentinel.read_text(encoding="utf-8"),
        )
        self.assertTrue((self.snapshot / "ordinary-after-link.txt").is_file())

    @unittest.skipUnless(os.name == "nt", "Windows hard-link ACL cleanup probe")
    def test_snapshot_hardlink_is_unlinked_before_recursive_acl_cleanup(self) -> None:
        external_engine_file = self.guard / "immutable-engine.py"
        original = b"host-owned immutable engine content\r\n"
        external_engine_file.write_bytes(original)
        snapshot_alias = self.snapshot / "project-engine-alias.py"
        os.link(external_engine_file, snapshot_alias)
        self.assertGreater(external_engine_file.stat().st_nlink, 1)
        before_acl = subprocess.run(
            ["icacls", str(external_engine_file)], capture_output=True,
            text=True, check=True,
        ).stdout

        removed = windows_containment._remove_snapshot_reparse_entries(
            self.snapshot
        )

        self.assertIn("project-engine-alias.py", removed)
        self.assertFalse(snapshot_alias.exists())
        self.assertEqual(original, external_engine_file.read_bytes())
        self.assertEqual(1, external_engine_file.stat().st_nlink)
        after_acl = subprocess.run(
            ["icacls", str(external_engine_file)], capture_output=True,
            text=True, check=True,
        ).stdout
        self.assertEqual(before_acl, after_acl)

    def test_snapshot_cleanup_accepts_an_enumerated_file_that_really_disappeared(self) -> None:
        transient = self.snapshot / "browser-profile-wal"
        transient.write_bytes(b"pending browser profile cleanup")
        stable = self.snapshot / "stable.txt"
        stable.write_text("ordinary snapshot content", encoding="utf-8")
        real_stat = os.stat
        raced = False

        def disappearing_stat(path, *args, **kwargs):
            nonlocal raced
            if Path(path) == transient and not raced:
                raced = True
                transient.unlink()
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(
            windows_containment.os, "stat", side_effect=disappearing_stat,
        ):
            removed = windows_containment._remove_snapshot_reparse_entries(
                self.snapshot
            )

        self.assertTrue(raced)
        self.assertEqual([], removed)
        self.assertFalse(transient.exists())
        self.assertEqual("ordinary snapshot content", stable.read_text(encoding="utf-8"))

    def test_snapshot_cleanup_accepts_an_unsafe_alias_deleted_during_unlink(self) -> None:
        external = self.guard / "engine-owned.txt"
        external.write_bytes(b"engine-owned")
        alias = self.snapshot / "raced-engine-alias.txt"
        os.link(external, alias)
        real_unlink = os.unlink
        raced = False

        def disappearing_unlink(path, *args, **kwargs):
            nonlocal raced
            if Path(path) == alias and not raced:
                raced = True
                real_unlink(path, *args, **kwargs)
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(
            windows_containment.os, "unlink", side_effect=disappearing_unlink,
        ):
            removed = windows_containment._remove_snapshot_reparse_entries(
                self.snapshot
            )

        self.assertTrue(raced)
        self.assertEqual([], removed)
        self.assertFalse(alias.exists())
        self.assertEqual(b"engine-owned", external.read_bytes())

    def test_snapshot_cleanup_fails_closed_when_missing_alias_is_still_present(self) -> None:
        external = self.guard / "engine-owned.txt"
        external.write_bytes(b"engine-owned")
        alias = self.snapshot / "leftover-engine-alias.txt"
        os.link(external, alias)
        real_unlink = os.unlink

        def false_missing(path, *args, **kwargs):
            if Path(path) == alias:
                raise FileNotFoundError(2, "simulated missing entry", str(path))
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(
            windows_containment.os, "unlink", side_effect=false_missing,
        ):
            with self.assertRaises(FileNotFoundError):
                windows_containment._remove_snapshot_reparse_entries(self.snapshot)

        self.assertTrue(alias.exists())
        self.assertEqual(b"engine-owned", external.read_bytes())


if __name__ == "__main__":
    unittest.main()
