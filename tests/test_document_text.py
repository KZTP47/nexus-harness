from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from xml.sax.saxutils import escape
import zipfile

from our_harness import chat, document_text
from our_harness.agent_tools import AgentToolSession
from our_harness.bounded_file_read import read_file_page
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.memory import MemoryStore
from our_harness.models import HarnessError
from our_harness.workflow import WorkflowDeadline


def make_docx(text="Follow the document prompt: build the café tracker.", *, body=None, extras=None, namespace=None):
    ns = namespace or "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = body if body is not None else f"<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        if "word/document.xml" not in (extras or {}):
            archive.writestr("word/document.xml", f'<w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>')
        for name, data in (extras or {}).items():
            archive.writestr(name, data)
    return stream.getvalue()


class DocumentTextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="word-reader-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})

    def test_paragraphs_tables_links_revisions_breaks_and_unicode(self):
        body = ('<w:p><w:r><w:t>First café 🚀</w:t><w:tab/><w:t>value</w:t><w:br/><w:t>line</w:t></w:r></w:p>'
            '<w:tbl><w:tr><w:trPr/><w:tc><w:p><w:r><w:t>Key</w:t></w:r></w:p></w:tc>'
            '<w:tc><w:p><w:hyperlink><w:r><w:t>Destination</w:t></w:r></w:hyperlink></w:p></w:tc></w:tr></w:tbl>'
            '<w:p><w:del><w:r><w:delText>obsolete</w:delText></w:r></w:del>'
            '<w:ins><w:r><w:t>Current</w:t></w:r></w:ins><w:r><w:instrText>field code</w:instrText></w:r></w:p>')
        for ns in document_text._WORD_NAMESPACES:
            with self.subTest(namespace=ns):
                result = document_text.extract_docx_text(make_docx(body=body, namespace=ns))
                self.assertEqual(result, "First café 🚀\tvalue\nline\nKey\tDestination\nCurrent\n")

    def test_headers_footers_and_notes_are_included_but_metadata_is_not(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        extras = {f"word/{name}.xml": f'<w:root xmlns:w="{ns}"><w:p><w:r><w:t>{name} requirement</w:t></w:r></w:p></w:root>'
                  for name in ("header1", "footer1", "footnotes", "endnotes")}
        extras["docProps/core.xml"] = "not document text"
        result = document_text.extract_docx_text(make_docx(extras=extras))
        for name in ("header1", "footer1", "footnotes", "endnotes"):
            self.assertIn(name + " requirement", result)
        self.assertNotIn("not document text", result)

    def test_attachment_generic_mime_preserves_bytes_and_supplies_text(self):
        raw = make_docx()
        for name, mime in (("Task résumé.DOCX", "application/octet-stream"), ("uploaded", document_text.DOCX_MIME), ("prompt.docx", "text/plain")):
            with self.subTest(name=name, mime=mime):
                public, files, context = chat.keep_attachments(self.config, "arbitrary-route", [{
                    "name": name, "type": mime, "data": base64.b64encode(raw).decode(),
                }], "independent-chat")
                self.assertIn("Follow the document prompt: build the café tracker.", context)
                self.assertIn("ATTACHED WORD DOCUMENT", context)
                self.assertEqual(public[0]["type"], document_text.DOCX_MIME)
                self.assertEqual(public[0]["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(Path(files[0]["path"]).read_bytes(), raw)
                self.assertEqual(base64.b64decode(files[0]["data"]), raw)

    def test_invalid_empty_and_oversized_documents_fail_before_saving(self):
        for raw in (b"not a Word document", make_docx(body=""), make_docx(extras={"word/document.xml": "<broken"})):
            with self.subTest(raw=raw[:20]):
                with self.assertRaises(chat.ChatError):
                    chat.keep_attachments(self.config, "", [{"name": "bad.docx", "data": base64.b64encode(raw).decode()}])
        with mock.patch.object(chat, "MOST_ATTACHMENT_TEXT", 5):
            with self.assertRaisesRegex(chat.ChatError, "did not clip"):
                chat.keep_attachments(self.config, "", [{"name": "big.docx", "data": base64.b64encode(make_docx()).decode()}])
        self.assertFalse(list(self.root.rglob("*.docx")))

    def test_untrusted_xml_and_archive_limits_are_enforced(self):
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        xml = f'<!DOCTYPE x [<!ENTITY x "expanded">]><w:document xmlns:w="{ns}"><w:p><w:r><w:t>&x;</w:t></w:r></w:p></w:document>'
        for encoding in ("utf-8", "utf-16"):
            with self.subTest(encoding=encoding):
                with self.assertRaisesRegex(HarnessError, "document type"):
                    document_text.extract_docx_text(make_docx(extras={"word/document.xml": xml.encode(encoding)}))
        with self.assertRaisesRegex(HarnessError, "nesting"):
            document_text.extract_docx_text(make_docx(body="<w:p>" * 150 + "</w:p>" * 150))
        for constant, limit in (("MAX_DOCX_XML_BYTES", 20), ("MAX_DOCX_MEMBERS", 1), ("MAX_DOCX_TEXT", 2)):
            with mock.patch.object(document_text, constant, limit):
                with self.assertRaises(HarnessError):
                    document_text.extract_docx_text(make_docx())

    def test_docx_pages_resume_after_restart_and_invalidate_on_source_or_contract_change(self):
        raw = make_docx("café 🚀 repeated " * 250)
        args = {"path": "specs/Task.docx", "start_line": 1, "end_line": 100, "max_bytes": 120}
        limits = {"output_limit": 1024, "configured_output_limit": 1024, "max_file_bytes": 1_000_000}
        page = read_file_page(raw, args, **limits)
        first = page
        pieces = [page["content"]]
        while page["next_cursor"]:
            args = json.loads(json.dumps({**args, "cursor": page["next_cursor"]}))
            page = read_file_page(raw, args, **limits)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()), 1024)
            pieces.append(page["content"])
        self.assertEqual("".join(pieces), document_text.extract_docx_text(raw))
        self.assertEqual(first["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(first["format"], "docx")
        args["cursor"] = first["next_cursor"]
        with self.assertRaisesRegex(HarnessError, "invalid or stale"):
            read_file_page(make_docx("changed"), args, **limits)
        with mock.patch("our_harness.bounded_file_read.DOCX_TEXT_VERSION", 2):
            with self.assertRaisesRegex(HarnessError, "invalid or stale"):
                read_file_page(raw, args, **limits)

    def test_agent_tool_reads_docx_in_arbitrary_project_and_keeps_path_boundary(self):
        (self.root / "requirements.docx").write_bytes(make_docx())
        with MemoryStore(self.config) as memory:
            session = AgentToolSession(self.config, memory, WorkflowDeadline.start(10), lambda *_: None)
            args = {"path": "requirements.docx", "start_line": 1, "end_line": 20, "max_bytes": 4000}
            result = session.execute("planner", "read-word", "read_file", args)
            self.assertEqual(result["status"], "ok", result)
            self.assertIn("build the café tracker", json.loads(result["content"])["content"])
            denied = session.execute("planner", "outside-word", "read_file", {**args, "path": "../private.docx"})
            self.assertEqual(denied["status"], "error")
