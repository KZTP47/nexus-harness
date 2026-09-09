from __future__ import annotations

import base64
import copy
from contextlib import nullcontext
import hashlib
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock
import zipfile

from our_harness import chat, public_web, research_tools, research_chat, swarm_work, long_horizon
from our_harness.agent_tools import AgentToolSession
from our_harness.archive_tools import ZipInspection
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError, ProviderRequest, ProviderResponse
from our_harness.providers.base import _strict_output_schema
from our_harness.workflow import WorkflowDeadline
from our_harness.redaction import CredentialRedactor
from test_document_text import make_docx


def make_zip(files=None):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (files or {"bundle/SKILL.md": "# Skill\nUse resources/reference.md to build the requested tracker.",
                                      "bundle/resources/reference.md": "Tracker must preserve café names.",
                                      "bundle/prompt.docx": make_docx(), "bundle/logo.bin": b"\xff\x00"}).items():
            archive.writestr(name, content)
    return buffer.getvalue()


class ResearchToolsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nexus-research-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.raw = make_zip()
        self.path = self.root / "Project bundle.zip"
        self.path.write_bytes(self.raw)
        self.tools = research_tools.ResearchTools(self.root, read_project=lambda path: (self.root / path).read_bytes())

    def test_zip_listing_reading_docx_and_inspection_extraction_preserve_project(self):
        listing = self.tools.execute("list_archive", {"path": self.path.name})
        entries = [json.loads(line) for line in listing["content"].splitlines()]
        self.assertEqual(len(entries), 4)
        self.assertIn("bundle/SKILL.md", [one["member"] for one in entries])
        result = self.tools.execute("read_archive", {"path": self.path.name, "member": "bundle/resources/reference.md"})
        self.assertIn("café", result["content"])
        document = self.tools.execute("read_archive", {"path": self.path.name, "member": "bundle/prompt.docx"})
        self.assertIn("build the café tracker", document["content"])
        extracted = self.tools.execute("extract_archive", {"path": self.path.name})
        destination = Path(extracted["extracted_to"])
        self.assertIn(self.root / ".harness/archive-inspection", destination.parents)
        self.assertEqual((destination / "bundle/logo.bin").read_bytes(), b"\xff\x00")
        manifest = json.loads(destination.with_suffix(".manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["source_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertFalse((self.root / "bundle").exists())
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_archive_paths_links_duplicates_and_limits_fail_closed(self):
        for name in ("../escape.txt", "/absolute.txt", "C:/outside.txt", "a\\b.txt", "NUL.txt", "alias. /x", "x:stream", "name\x00hidden"):
            with self.subTest(name=name):
                # ZipFile removes NUL during writing; create a raw archive with that byte instead.
                raw = make_zip({name.replace("\x00", "X"): "data"})
                if "\x00" in name:
                    raw = raw.replace(name.replace("\x00", "X").encode(), name.encode())
                if "\\" in name:
                    raw = raw.replace(name.replace("\\", "/").encode(), name.encode())
                with self.assertRaises(HarnessError):
                    ZipInspection(raw)
        for files in ({"A.txt": "one", "a.txt": "two"}, {"file": "one", "file/child.txt": "two"}):
            with self.assertRaises(HarnessError):
                ZipInspection(make_zip(files))
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("link")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../outside")
        with self.assertRaisesRegex(HarnessError, "links"):
            ZipInspection(buffer.getvalue())
        for constant, maximum in (("MAX_ARCHIVE_BYTES", 10), ("MAX_EXPANDED_BYTES", 10), ("MAX_ARCHIVE_ENTRIES", 1)):
            with mock.patch("our_harness.archive_tools." + constant, maximum):
                with self.assertRaises(HarnessError):
                    ZipInspection(self.raw)
        with self.assertRaises(HarnessError):
            ZipInspection(b"not zip")

    def test_extraction_failure_removes_partial_copy_and_never_overwrites_existing_files(self):
        archive = ZipInspection(self.raw)
        self.addCleanup(archive.close)
        with mock.patch.object(archive, "read", side_effect=[b"first", HarnessError("damaged")]):
            with self.assertRaisesRegex(HarnessError, "damaged"):
                archive.extract(self.root)
        self.assertEqual(list((self.root / ".harness/archive-inspection").iterdir()), [])
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_encrypted_and_corrupted_zip_members_are_explicit_errors(self):
        raw = make_zip({"file.txt": "original contents"})
        encrypted = bytearray(raw)
        central = raw.index(b"PK\x01\x02")
        encrypted[central + 8] |= 1
        with self.assertRaisesRegex(HarnessError, "encrypted"):
            ZipInspection(bytes(encrypted))
        damaged = bytearray(raw)
        damaged[30 + len("file.txt")] ^= 0xFF
        archive = ZipInspection(bytes(damaged))
        self.addCleanup(archive.close)
        with self.assertRaisesRegex(HarnessError, "damaged"):
            archive.read("file.txt")

    def test_public_zip_url_and_changed_web_source_use_bounded_pages(self):
        url = "https://example.test/archive.zip"
        with mock.patch.object(research_tools, "fetch_public", return_value={"url": url, "data": self.raw, "content_type": "application/zip"}):
            listing = self.tools.execute("list_archive", {"path": url})
            self.assertIn("bundle/SKILL.md", listing["content"])
            result = self.tools.execute("read_archive", {"path": url, "member": "bundle/resources/reference.md"})
            self.assertIn("café", result["content"])
        args = {"url": "https://example.test/source.md", "max_bytes": 20}
        with mock.patch.object(research_tools, "fetch_public", return_value={"url": args["url"], "data": b"source " * 100, "content_type": "text/plain"}):
            page = self.tools.execute("fetch_url", args)
        with mock.patch.object(research_tools, "fetch_public", return_value={"url": args["url"], "data": b"changed source " * 100, "content_type": "text/plain"}):
            with self.assertRaisesRegex(HarnessError, "invalid or stale"):
                research_tools.ResearchTools(self.root).execute("fetch_url", {**args, "cursor": page["next_cursor"]})

    def test_invalid_paging_is_rejected_without_network_access(self):
        with mock.patch.object(research_tools, "fetch_public") as fetch:
            with self.assertRaises(HarnessError):
                self.tools.execute("fetch_url", {"url": "https://example.test", "max_bytes": False})
        fetch.assert_not_called()

    def test_zip_attachment_is_lazy_digest_bound_and_survives_new_tool_session(self):
        public, files, context = chat.keep_attachments(self.config, "route-a", [{
            "name": "Bundle.ZIP", "type": "application/octet-stream", "data": base64.b64encode(self.raw).decode(),
        }], "chat-a")
        identity = "attachment://" + public[0]["sha256"]
        self.assertIn(identity, context)
        self.assertNotIn("Tracker must", context)
        persisted = [{key: value for key, value in one.items() if key != "data"} for one in files]
        for records in (files, persisted):
            tools = research_tools.ResearchTools(self.root, attachments=json.loads(json.dumps(records)))
            result = tools.execute("read_archive", {"path": identity, "member": "bundle/resources/reference.md"})
            self.assertIn("café", result["content"])
        with self.assertRaisesRegex(HarnessError, "not attached"):
            research_tools.ResearchTools(self.root).execute("list_archive", {"path": identity})
        Path(persisted[0]["path"]).write_bytes(make_zip({"other": "changed"}))
        with self.assertRaisesRegex(HarnessError, "changed"):
            research_tools.ResearchTools(self.root, attachments=persisted).execute("list_archive", {"path": identity})

    def test_pages_reconstruct_after_restart_and_reject_changed_archive(self):
        self.path.write_bytes(make_zip({"long.txt": "café 🚀\n" * 150}))
        args = {"path": self.path.name, "member": "long.txt", "max_bytes": 200}
        first = self.tools.execute("read_archive", args, output_limit=1400)
        page, pieces = first, [first["content"]]
        while page["next_cursor"]:
            args["cursor"] = page["next_cursor"]
            restarted = research_tools.ResearchTools(self.root, read_project=lambda p: (self.root / p).read_bytes())
            page = restarted.execute("read_archive", args, output_limit=1400)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()), 1400)
            pieces.append(page["content"])
        self.assertEqual("".join(pieces), "café 🚀\n" * 150)
        self.path.write_bytes(make_zip({"long.txt": "café 🚀\n" * 150, "new.txt": "changed archive"}))
        with self.assertRaisesRegex(HarnessError, "invalid or stale"):
            self.tools.execute("read_archive", {**args, "cursor": first["next_cursor"]})

    def test_agent_session_and_work_schemas_expose_research_with_existing_boundaries(self):
        for response_format in (swarm_work.WORK_FORMAT, long_horizon.AGENT_ACTION_FORMAT):
            strict = _strict_output_schema(response_format.schema)
            variants = strict["properties"]["tool_calls"]["items"]["anyOf"]
            names = {one["properties"]["name"]["enum"][0] for one in variants}
            self.assertTrue(research_tools.RESEARCH_TOOL_NAMES <= names)
            for one in variants:
                self.assertFalse(one["properties"]["arguments"]["additionalProperties"])
        with MemoryStore(self.config) as memory:
            session = AgentToolSession(self.config, memory, WorkflowDeadline.start(10), lambda *_: None)
            self.assertTrue(research_tools.RESEARCH_TOOL_NAMES <= {one["name"] for one in session.definitions("planner")})
            allowed = session.execute("planner", "zip-1", "list_archive", {"path": self.path.name})
            self.assertEqual(allowed["status"], "ok", allowed)
            denied = session.execute("planner", "zip-2", "extract_archive", {"path": "../outside.zip"})
            self.assertEqual(denied["status"], "error")

    def test_durable_tool_receipt_replays_after_restart_and_invalidates_changed_configuration(self):
        with MemoryStore(self.config) as memory:
            memory.ensure_external_run("research-run", "Read a ZIP", "research-test")
            session = AgentToolSession(self.config, memory, WorkflowDeadline.start(10), lambda *_: None, run_id="research-run")
            args = {"path": self.path.name}
            first = session.execute("planner", "archive-list", "list_archive", args, execution_scope="response-one")
            self.assertEqual(first["status"], "ok", first)
            checkpoint = session.budget_state()
            restarted = AgentToolSession(self.config, memory, WorkflowDeadline.start(10), lambda *_: None, run_id="research-run")
            restarted.restore_budget_state(checkpoint)
            with mock.patch.object(restarted.research_tools, "execute", side_effect=AssertionError("should replay")):
                replayed = restarted.execute("planner", "archive-list", "list_archive", args, execution_scope="response-one")
            self.assertTrue(replayed["replayed"])
            self.assertEqual(replayed["content"], first["content"])
            self.config.data["workflow"]["max_tool_output_bytes"] = 2000
            changed = AgentToolSession(self.config, memory, WorkflowDeadline.start(10), lambda *_: None, run_id="research-run")
            changed.restore_budget_state(checkpoint)
            stale = changed.execute("planner", "archive-list", "list_archive", args, execution_scope="response-one")
            self.assertEqual(stale["status"], "error")
            fresh = changed.execute("planner", "archive-list-new", "list_archive", args, execution_scope="response-one")
            self.assertEqual(fresh["status"], "ok", fresh)
            self.assertLessEqual(fresh["content_bytes"], 2000)

    def test_github_discovery_loads_immutable_skill_and_supporting_resource(self):
        sha, tree = "a" * 40, "b" * 40
        def fetch(url):
            if "/search/repositories?" in url:
                data = {"items": [{"full_name": "fixture/skills", "html_url": "https://github.com/fixture/skills"}], "total_count": 1}
            elif "/commits/" in url:
                data = {"sha": sha, "commit": {"tree": {"sha": tree}}}
            elif "/git/trees/" in url:
                data = {"tree": [{"type": "blob", "path": "skills/tracker/SKILL.md"}], "truncated": False}
            else:
                return {"url": url, "data": ("Use references/spec.md to build the tracker." if url.endswith("SKILL.md") else "Keep Unicode names.").encode(), "content_type": "text/plain"}
            return {"url": url, "data": json.dumps(data).encode(), "content_type": "application/json"}
        with mock.patch.object(research_tools, "fetch_public", side_effect=fetch):
            search = self.tools.execute("search_github", {"query": "tracker skills"})
            self.assertIn("fixture/skills", search["content"])
            found = self.tools.execute("github_skills", {"repository": "fixture/skills", "ref": "feature/branch"})
            url = json.loads(found["content"])["url"]
            self.assertIn(sha, url)
            skill = self.tools.execute("load_skill", {"url": url})
            self.assertIn("build the tracker", skill["content"])
            support = self.tools.execute("fetch_url", {"url": skill["resource_base_url"] + "references/spec.md"})
            self.assertIn("Unicode", support["content"])

    def test_ordinary_chat_research_loop_reads_zip_and_returns_grounded_answer(self):
        _public, files, context = chat.keep_attachments(self.config, "route", [{"name": "input.zip", "data": base64.b64encode(self.raw).decode()}])
        identity = "attachment://" + files[0]["sha256"]
        requests = []
        def complete(request, phase):
            requests.append(request)
            if len(requests) == 1:
                self.assertIn("AVAILABLE RESEARCH TOOLS", request.system_prefix)
                return ProviderResponse(json.dumps({"nexus_research_tool": {"name": "read_archive", "arguments": {"path": identity, "member": "bundle/resources/reference.md"}}}), "stop")
            self.assertIn("Tracker must preserve café names.", request.messages[-1]["content"])
            return ProviderResponse("The tracker must preserve café names.", "stop")
        response = research_chat.complete_research_chat(self.config, ProviderRequest("policy", context, [{"role": "user", "content": "Read the attached archive"}], "fixture", attachments=files), complete)
        self.assertEqual(response.text, "The tracker must preserve café names.")
        self.assertEqual(len(requests), 2)

    def test_ordinary_chat_cannot_use_research_to_read_unattached_project_files(self):
        tools = research_tools.ResearchTools(self.root)
        with self.assertRaisesRegex(HarnessError, "Select project work"):
            tools.execute("read_archive", {"path": self.path.name, "member": "bundle/SKILL.md"})

    def test_saved_chat_can_read_zip_and_follow_up_without_reattaching(self):
        public, files, context = chat.keep_attachments(self.config, "route", [{
            "name": "input.zip", "data": base64.b64encode(self.raw).decode()}], "zip-chat")
        identity = "attachment://" + files[0]["sha256"]
        provider = mock.Mock()
        requests = []
        def complete(request):
            requests.append(request)
            self.assertIn(identity, request.system_prefix)
            if len(requests) % 2:
                return ProviderResponse(json.dumps({"nexus_research_tool": {"name": "read_archive", "arguments": {
                    "path": identity, "member": "bundle/resources/reference.md"}}}), "stop")
            self.assertIn("Tracker must preserve café names.", request.messages[-1]["content"])
            return ProviderResponse("The ZIP specifies preserving café names.", "stop")
        provider.complete.side_effect = complete
        with mock.patch("our_harness.swarm_runs.provider_effect", return_value=nullcontext()):
            first = chat._ask_and_keep(self.config, "route", "Read the ZIP", provider, "fixture", CredentialRedactor(self.config), "route", "zip-chat",
                dynamic_context=context, provider_attachments=files, kept_attachments=public)
            second = chat._ask_and_keep(self.config, "route", "Read the requirement again", provider, "fixture", CredentialRedactor(self.config), "route", "zip-chat")
        self.assertEqual(len(requests), 4)
        self.assertIn("café", str(first))
        self.assertIn("café", str(second))


class PublicWebTests(unittest.TestCase):
    def test_private_and_special_urls_are_denied_before_connect(self):
        for url in ("file:///secret", "https://user:password@example.test/", "http://example.test:8080/", "https://example.test/\nHeader:secret"):
            with self.assertRaises(HarnessError):
                public_web.public_url(url)
        for address in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "0.0.0.0"):
            with mock.patch.object(public_web.socket, "getaddrinfo", return_value=[(None, None, None, None, (address, 80))]):
                with self.assertRaises(HarnessError):
                    public_web._public_addresses("private.test", 80)

    def test_redirect_revalidates_destination_and_transport_has_no_auth_headers(self):
        first = mock.Mock(status=302)
        first.getheader.side_effect = lambda key, default=None: "http://127.0.0.1/private" if key == "Location" else default
        connection = mock.Mock()
        connection.getresponse.return_value = first
        def resolve(host, port, **kwargs):
            return [(None, None, None, None, ("127.0.0.1" if host == "127.0.0.1" else "93.184.216.34", port))]
        with mock.patch.object(public_web.socket, "getaddrinfo", side_effect=resolve), \
                mock.patch.object(public_web.socket, "create_connection") as connected, \
                mock.patch.object(public_web.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(HarnessError, "private"):
                public_web.fetch_public("http://public.test/start")
        self.assertEqual(connected.call_args.args[0], ("93.184.216.34", 80))
        self.assertEqual(connected.call_count, 1)
        headers = connection.request.call_args.kwargs["headers"]
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)

    def test_html_has_followable_links_without_scripts_and_download_is_bounded(self):
        rendered = public_web.web_text({"url": "https://example.test/folder/page", "content_type": "text/html", "data": b'<script>hidden()</script><h1>Skills</h1><a href="../SKILL.md">Read skill</a>'}).decode()
        self.assertNotIn("hidden", rendered)
        self.assertIn("https://example.test/SKILL.md", rendered)
        response = mock.Mock(status=200)
        response.getheader.side_effect = lambda key, default=None: default
        response.read1.return_value = b"too many bytes"
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(public_web, "_public_addresses", return_value=["93.184.216.34"]), \
                mock.patch.object(public_web.socket, "create_connection"), \
                mock.patch.object(public_web.http.client, "HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(HarnessError, "size"):
                public_web.fetch_public("http://example.test", max_bytes=4)
