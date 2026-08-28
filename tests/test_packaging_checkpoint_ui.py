from __future__ import annotations

import json
import http.client
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from html.parser import HTMLParser
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace

from our_harness.audit import audit_distribution, audit_installed_distribution
from our_harness.checkpoints import CheckpointManager
from our_harness.config import DEFAULT_CONFIG, load_config, load_isolated_config
from our_harness.server import HarnessHTTPServer, loopback_url


ROOT = Path(__file__).resolve().parents[1]


def create_directory_link(link: Path, target: Path) -> None:
    denied: OSError | None = None
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as exc:
        denied = exc
        if sys.platform != "win32":
            raise
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise denied


class ElementCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class PackagingTests(unittest.TestCase):
    def test_ipv6_loopback_url_uses_brackets(self) -> None:
        self.assertEqual(loopback_url("::1", 8765), "http://[::1]:8765")

    @unittest.skipUnless(sys.platform == "win32" and shutil.which("powershell"), "Windows PowerShell is required")
    def test_windows_launcher_preserves_non_ascii_paths_without_path_changes(self) -> None:
        helper = ROOT / "scripts" / "install_helpers.ps1"
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "räksmörgås-工具"
            bin_root = folder / "bin"
            app_root = folder / "app"
            bin_root.mkdir(parents=True)
            app_root.mkdir(parents=True)
            launcher = bin_root / "harness.cmd"
            powershell_launcher = bin_root / "harness-launcher.ps1"
            application_path = app_root / "harness.pyz"

            build = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "build_zipapp.py"), "--output", str(application_path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(build.returncode, 0, build.stderr)

            def quoted(value: Path) -> str:
                return str(value).replace("'", "''")

            command = (
                f". '{quoted(helper)}'; "
                f"Write-HarnessLauncher -Path '{quoted(launcher)}'"
            )
            before_path = os.environ.get("PATH")
            generated = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            raw = launcher.read_bytes()
            text = raw.decode("ascii")
            self.assertIn('"%~dp0harness-launcher.ps1"', text)
            self.assertNotIn(str(folder).encode("utf-8"), raw)
            powershell_text = powershell_launcher.read_text(encoding="utf-8")
            self.assertNotIn(str(folder), powershell_text)
            self.assertNotIn(str(Path(sys.executable)), powershell_text)

            moved = Path(temporary) / "relocated-å·¥å…·"
            folder.rename(moved)
            launcher = moved / "bin" / "harness.cmd"

            result = subprocess.run(
                f'cmd.exe /d /s /c ""{launcher}" --version"',
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("harness 0.2.0", result.stdout)
            self.assertEqual(os.environ.get("PATH"), before_path)

    def test_shell_launcher_resolves_sibling_application_without_absolute_paths(self) -> None:
        launcher = (ROOT / "scripts" / "harness-launcher.sh").read_text(encoding="utf-8")
        self.assertIn('../app/harness.pyz', launcher)
        self.assertIn('command -v python3', launcher)
        self.assertNotIn(str(ROOT), launcher)

    def test_zipapp_launch_and_noninteractive_init(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / "harness.pyz"
            project = root / "project with spaces"
            project.mkdir()
            (project / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1'\n", encoding="utf-8")
            build = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "build_zipapp.py"), "--output", str(output)],
                cwd=ROOT, capture_output=True, text=True, check=False,
            )
            self.assertEqual(build.returncode, 0, build.stderr)
            version = subprocess.run([sys.executable, str(output), "--version"], capture_output=True, text=True, check=False)
            self.assertEqual(version.returncode, 0, version.stderr)
            self.assertIn("harness 0.2.0", version.stdout)
            audit = subprocess.run([sys.executable, str(output), "audit"], capture_output=True, text=True, check=False)
            self.assertEqual(audit.returncode, 0, audit.stderr)
            audit_result = json.loads(audit.stdout)
            self.assertEqual(audit_result["mode"], "installed")
            self.assertGreater(audit_result["scanned_files"], 0)
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertFalse(any("__pycache__" in name or name.endswith((".pyc", ".pyo")) for name in names))
                self.assertFalse(any(".egg-info/" in name or name.startswith(("build/", "dist/")) for name in names))
                expected = {
                    path.relative_to(ROOT / "src").as_posix(): path.read_bytes()
                    for path in (ROOT / "src" / "our_harness").rglob("*")
                    if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}
                }
                for name, content in expected.items():
                    self.assertIn(name, names)
                    self.assertEqual(archive.read(name), content, name)
            for command in ([sys.executable, str(output), "runs", "--help"], [sys.executable, str(output), "benchmark", "--help"]):
                capability = subprocess.run(command, capture_output=True, text=True, check=False)
                self.assertEqual(capability.returncode, 0, capability.stderr)
            init = subprocess.run(
                [sys.executable, str(output), "init", str(project), "--yes", "--provider", "ollama"],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(init.returncode, 0, init.stderr)
            config_text = (project / ".harness" / "config.json").read_text(encoding="utf-8")
            self.assertNotIn(str(project), config_text)

    def test_schema_covers_default_top_level_keys(self) -> None:
        schema = json.loads((ROOT / "harness.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(set(DEFAULT_CONFIG), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])

    def test_distribution_audit(self) -> None:
        result = audit_distribution(ROOT)
        self.assertTrue(result["passed"], result["findings"])
        self.assertGreater(result["scanned_files"], 0)

    def test_distribution_audit_fails_closed_and_detects_general_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            absent = audit_distribution(root)
            self.assertFalse(absent["passed"])
            package = root / "src" / "our_harness"
            package.mkdir(parents=True)
            (package / "module.py").write_text('LOCATION = "/opt/company/repository"\n', encoding="utf-8")
            result = audit_distribution(root)
            self.assertFalse(result["passed"])
            self.assertTrue(any(item["message"] == "machine-specific absolute path" for item in result["findings"]))

    def test_installed_resource_audit_scans_package(self) -> None:
        result = audit_installed_distribution()
        self.assertTrue(result["passed"], result["findings"])
        self.assertEqual(result["mode"], "installed")
        self.assertGreater(result["scanned_files"], 0)


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_captures_untracked_and_restores_through_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "pyproject.toml").write_text("[project]\nname='fixture'\nversion='1'\n", encoding="utf-8")
            path = root / "note.txt"
            path.write_text("before\n", encoding="utf-8")
            manager = CheckpointManager(load_config(root))
            checkpoint = manager.create("before edit")
            path.write_text("after\n", encoding="utf-8")
            result = manager.restore_file(checkpoint["id"], "note.txt")
            self.assertEqual(path.read_text(encoding="utf-8"), "before\n")
            self.assertTrue(result["transaction_id"])
            self.assertEqual(manager.list()[0]["note"], "before edit")

    def test_checkpoint_relocates_and_restores_binary_bytes_and_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            original = base / "original"
            relocated = base / "relocated"
            original.mkdir()
            relocated.mkdir()
            payload = b"\x00\xff\x80binary\r\n\x00"
            source = original / "asset.bin"
            source.write_bytes(payload)
            source.chmod(0o744)
            captured_mode = stat.S_IMODE(source.stat().st_mode)

            checkpoint = CheckpointManager(load_config(original)).create("binary relocation")
            archive_path = Path(checkpoint["archive"])
            with zipfile.ZipFile(archive_path) as archive:
                manifest_raw = archive.read("manifest.json")
                manifest = json.loads(manifest_raw)
            record = next(item for item in manifest["files"] if item["path"] == "asset.bin")
            self.assertEqual(record["mode"], captured_mode)
            self.assertNotIn(str(original).encode("utf-8"), manifest_raw)

            relocated_folder = relocated / ".harness" / "checkpoints"
            relocated_folder.mkdir(parents=True)
            shutil.copy2(archive_path, relocated_folder / archive_path.name)
            target = relocated / "asset.bin"
            target.write_bytes(b"changed")
            target.chmod(0o600)
            result = CheckpointManager(load_config(relocated)).restore_file(checkpoint["id"], "asset.bin")

            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), captured_mode)
            self.assertTrue(result["transaction_id"])
            self.assertFalse(list(relocated.glob(".asset.bin.*.tmp")))

    def test_checkpoint_does_not_traverse_linked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "project"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "inside.txt").write_text("inside", encoding="utf-8")
            (outside / "secret.bin").write_bytes(b"outside")
            try:
                create_directory_link(root / "linked", outside)
            except OSError as exc:
                self.skipTest(f"directory link creation denied: {exc}")

            checkpoint = CheckpointManager(load_config(root)).create()
            with zipfile.ZipFile(checkpoint["archive"]) as archive:
                names = archive.namelist()
            self.assertIn("files/inside.txt", names)
            self.assertNotIn("files/linked/secret.bin", names)


class UITests(unittest.TestCase):
    def test_http_authority_origin_and_event_token_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = load_isolated_config(Path(temporary), {"ui": {"host": "127.0.0.1", "port": 0, "open_browser": False}})
            server = HarnessHTTPServer(("127.0.0.1", 0), config)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port

            def request(path: str, headers: dict[str, str]) -> tuple[int, dict]:
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                body = json.loads(response.read())
                connection.close()
                return response.status, body

            try:
                for bad_host in ("attacker.example", f"attacker.example:{port}", f"localhost.evil:{port}", "[::1"):
                    with self.subTest(host=bad_host):
                        status, body = request("/api/bootstrap", {"Host": bad_host})
                        self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                        self.assertIn("Host", body["error"])

                with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
                    connection.sendall(
                        (
                            "GET /api/bootstrap HTTP/1.1\r\n"
                            f"Host: 127.0.0.1:{port}\r\n"
                            f"Host: attacker.example:{port}\r\n"
                            "Connection: close\r\n\r\n"
                        ).encode("ascii")
                    )
                    raw = connection.recv(4096)
                self.assertTrue(raw.startswith(b"HTTP/1.1 400"), raw[:100])

                allowed_host = f"127.0.0.1:{port}"
                status, body = request(
                    "/api/bootstrap",
                    {"Host": allowed_host, "Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
                )
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertIn("Cross-site", body["error"])

                # A program asking directly, with none of the lines a browser
                # always sends, is not handed the key to the panel.
                status, body = request("/api/bootstrap", {"Host": allowed_host})
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertIn("control panel page", body["error"])

                status, bootstrap = request(
                    "/api/bootstrap", {"Host": allowed_host, "Sec-Fetch-Site": "same-origin"}
                )
                self.assertEqual(status, HTTPStatus.OK)
                token = bootstrap["token"]
                self.assertTrue(bootstrap["started_id"])

                status, body = request("/api/events?after=0", {"Host": allowed_host})
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertIn("token", body["error"])

                allowed_origin = f"http://127.0.0.1:{port}"
                status, body = request(
                    "/api/events?after=0",
                    {
                        "Host": allowed_host,
                        "Origin": allowed_origin,
                        "Sec-Fetch-Site": "same-origin",
                        "X-Harness-Token": token,
                    },
                )
                self.assertEqual(status, HTTPStatus.OK)
                self.assertEqual(body["events"], [])
                self.assertTrue(body["started_id"])

                status, body = request(
                    "/api/events?after=0",
                    {"Host": allowed_host, "Origin": "http://attacker.example", "X-Harness-Token": token},
                )
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertIn("Cross-origin", body["error"])

                status, body = request(
                    "/api/events?after=0",
                    {
                        "Host": allowed_host,
                        "Origin": f"http://localhost:{port}",
                        "X-Harness-Token": token,
                    },
                )
                self.assertEqual(status, HTTPStatus.BAD_REQUEST)
                self.assertIn("Cross-origin", body["error"])

                for loopback_authority in (f"localhost:{port}", f"[::1]:{port}"):
                    with self.subTest(loopback_authority=loopback_authority):
                        status, body = request(
                            "/api/bootstrap",
                            {"Host": loopback_authority, "Sec-Fetch-Site": "same-origin"},
                        )
                        self.assertEqual(status, HTTPStatus.OK)
                        self.assertIn("token", body)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_event_bus_bounds_serialized_bytes_and_keeps_monotonic_sequences(self) -> None:
        from our_harness.server import EventBus

        bus = EventBus(max_events=3, max_bytes=320)
        for index in range(5):
            bus.add({"kind": "small", "node": str(index), "payload": {"value": index}})
        retained = bus.after(0)
        self.assertLessEqual(len(retained), 3)
        self.assertEqual([event["sequence"] for event in retained], sorted(event["sequence"] for event in retained))
        self.assertEqual(retained[-1]["sequence"], 5)

        bus.add({"kind": "large", "node": "large", "payload": {"text": "é" * 5000}})
        retained = bus.after(0)
        self.assertEqual(retained[-1]["sequence"], 6)
        self.assertEqual(retained[-1]["kind"], "event_omitted")
        self.assertGreater(retained[-1]["payload"]["original_bytes"], bus.max_bytes)
        self.assertLessEqual(bus._bytes, bus.max_bytes)
        self.assertLessEqual(len(retained), bus.max_events)

    def test_workspace_run_reservation_is_exclusive(self) -> None:
        from our_harness.server import HarnessHandler, HarnessHTTPServer

        server = SimpleNamespace(run_lock=threading.Lock())
        server.reserve_run = lambda: HarnessHTTPServer.reserve_run(server)
        server.release_run = lambda: HarnessHTTPServer.release_run(server)
        self.assertTrue(HarnessHTTPServer.reserve_run(server))
        self.assertFalse(HarnessHTTPServer.reserve_run(server))

        responses = []
        handler = object.__new__(HarnessHandler)
        handler.server = server
        handler.path = "/api/run"
        handler._authorize = lambda: None
        handler._body = lambda: {"task": "one", "dry_run": False}
        handler._json = lambda value, status=200: responses.append((value, status))
        handler.send_error = lambda status: self.fail(f"unexpected HTTP error {status}")
        HarnessHandler.do_POST(handler)
        self.assertEqual(responses, [({"error": "A workspace run is already active"}, HTTPStatus.CONFLICT)])

        HarnessHTTPServer.release_run(server)
        self.assertTrue(HarnessHTTPServer.reserve_run(server))
        HarnessHTTPServer.release_run(server)

    def test_semantic_and_keyboard_surfaces_exist(self) -> None:
        html = (ROOT / "src" / "our_harness" / "ui" / "index.html").read_text(encoding="utf-8")
        parser = ElementCollector()
        parser.feed(html)
        tags = [tag for tag, _ in parser.elements]
        self.assertIn("main", tags)
        self.assertIn("nav", tags) if "nav" in tags else self.assertIn("aside", tags)
        self.assertTrue(any(attrs.get("role") == "application" for _, attrs in parser.elements))
        self.assertTrue(any(attrs.get("aria-live") == "polite" for _, attrs in parser.elements))
        self.assertTrue(any(tag == "table" for tag, _ in parser.elements))
        script = (ROOT / "src" / "our_harness" / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('dryRunInput").checked, graph', script)
        for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Escape", "Delete"):
            self.assertIn(key, script)

    def test_reduced_motion_and_focus_styles_exist(self) -> None:
        css = (ROOT / "src" / "our_harness" / "ui" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("prefers-reduced-motion", css)
        self.assertIn(":focus-visible", css)
        self.assertIn("--focus", css)


if __name__ == "__main__":
    unittest.main()
