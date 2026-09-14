"""Portable original-input history, bounded projection and unavailable-file behavior."""
import base64
import copy
import io
import json
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

from our_harness import chat, chat_attachment_context as context
from our_harness.config import DEFAULT_CONFIG, LoadedConfig
from our_harness.models import HarnessError, ProviderResponse
from our_harness.redaction import CredentialRedactor
from our_harness.research_tools import ResearchTools
from test_document_text import make_docx

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jD1sAAAAASUVORK5CYII=")


def wire(name="spec.txt", raw=b"ORIGINAL_UNIQUE_EVIDENCE", mime="text/plain"):
    return {"name": name, "type": mime, "data": base64.b64encode(raw).decode()}


class ChatAttachmentContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = LoadedConfig(copy.deepcopy(DEFAULT_CONFIG), self.root, [], {})
        self.config.data["provider"].update(name="openai-compatible", model="fixture", api_key_env="", endpoint="http://127.0.0.1:1", api_mode="chat-completions")
        self.requests = []
        self.provider = mock.Mock(complete=self.complete)
        patch = mock.patch.object(chat, "create_provider", return_value=self.provider)
        patch.start()
        self.addCleanup(patch.stop)

    def complete(self, request):
        self.requests.append(request)
        return ProviderResponse(text="I received your message.", finish_reason="stop")

    def send(self, files=None, text="Inspect the original again", filed_as="arbitrary-shared-chat"):
        return chat.say(self.config, "", text, attachments=files, filed_as=filed_as)

    def shown(self):
        return self.requests[-1].dynamic_context + json.dumps(self.requests[-1].messages)

    def original(self):
        turn = chat.read_it(self.config, "", "arbitrary-shared-chat")[0]
        return chat.attachment_path(self.config, "", "arbitrary-shared-chat", turn.attachments[0]["id"])[0]

    def test_plain_followup_retains_user_and_assistant_history(self):
        self.send(text="REMEMBER_THIS_PLAIN_TEXT")
        self.send(text="What did I say?")
        messages = self.requests[-1].messages
        self.assertEqual(messages[0], {"role": "user", "content": "REMEMBER_THIS_PLAIN_TEXT"})
        self.assertEqual(messages[1], {"role": "assistant", "content": "I received your message."})

    def test_plain_overbudget_history_is_serializable_and_disclosed(self):
        turns = [chat.Said("you", "x" * 900, "now"), chat.Said("them", "retained response", "now")]
        with mock.patch.object(chat, "CHAT_HISTORY_PROMPT_CHARACTERS", 800):
            messages = chat._project_chat_history(turns, speaker=None, filed_as="portable", route="arbitrary")
        self.assertIn("1 complete earlier turn(s)", messages[0]["content"])
        self.assertEqual(messages[1], {"role": "assistant", "content": "retained response"})
        self.assertNotIn("x" * 900, json.dumps(messages))

    def test_plain_projection_reserves_disclosure_without_slicing_turns(self):
        turns = [chat.Said("you", "earlier", "now"), chat.Said("them", "x" * 1000, "now")]
        with mock.patch.object(chat, "CHAT_HISTORY_PROMPT_CHARACTERS", 1000):
            messages = chat._project_chat_history(turns, speaker=None, filed_as="portable", route="route")
        self.assertLessEqual(sum(len(one["content"]) for one in messages), 1000)
        self.assertNotIn("x" * 100, json.dumps(messages))
        self.assertIn("2 complete earlier turn(s)", messages[0]["content"])

    def test_text_word_image_and_zip_survive_reopen_without_duplicate_files(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("nested/spec.txt", "ARCHIVE_EVIDENCE")
        files = [wire(), wire("plan.docx", make_docx("DOCX_ORIGINAL_EVIDENCE"), "application/octet-stream"), wire("screen.png", PNG, "image/png"), wire("bundle.zip", buffer.getvalue(), "application/zip")]
        self.send(files)
        before = sorted(str(path) for path in self.root.rglob("*"))
        self.config = LoadedConfig(copy.deepcopy(self.config.data), self.root, [], {})
        self.send()
        delivered = self.requests[-1].attachments
        self.assertEqual(len(delivered), 4)
        self.assertIn("ORIGINAL_UNIQUE_EVIDENCE", self.shown())
        self.assertIn("DOCX_ORIGINAL_EVIDENCE", self.shown())
        self.assertEqual(base64.b64decode(delivered[2]["data"]), PNG)
        archive = delivered[3]
        result = ResearchTools(self.root, attachments=delivered).execute("read_archive", {"path": "attachment://" + archive["sha256"], "member": "nested/spec.txt"})
        self.assertIn("ARCHIVE_EVIDENCE", str(result))
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob("*")))

    def test_shared_chat_storage_survives_changed_speaker_and_route(self):
        self.send([wire()])
        chat._ask_and_keep(self.config, "different-route", "Continue", self.provider, "fixture", CredentialRedactor(self.config), "different-route", "arbitrary-shared-chat", speaker={"id": "other", "name": "Other"})
        self.assertIn("ORIGINAL_UNIQUE_EVIDENCE", self.shown())
        self.assertEqual(len(self.requests[-1].attachments), 1)
        self.send(filed_as="foreign-chat")
        self.assertNotIn("ORIGINAL_UNIQUE_EVIDENCE", self.shown())
        self.assertEqual(self.requests[-1].attachments, [])

    def test_missing_tampered_and_hashless_originals_are_disclosed_not_dispatched(self):
        for kind in ("missing", "tampered", "hashless"):
            with self.subTest(kind=kind):
                name = "unavailable-" + kind
                self.send([wire()], filed_as=name)
                turns = chat.read_it(self.config, "", name)
                path, _ = chat.attachment_path(self.config, "", name, turns[0].attachments[0]["id"])
                if kind == "missing":
                    path.unlink()
                elif kind == "tampered":
                    path.write_bytes(b"CHANGED_PRIVATE_CONTENT")
                else:
                    turns[0].attachments[0].pop("sha256")
                    chat._keep_it(self.config, "", turns, name, replace_projection=True)
                result = self.send(filed_as=name)
                self.assertEqual(self.requests[-1].attachments, [])
                self.assertNotIn("CHANGED_PRIVATE_CONTENT", self.shown())
                self.assertEqual(result["attachment_context"]["omitted_files"], 1)
                self.assertEqual(result["answer"]["correlation"]["attachment_context"], result["attachment_context"])
                self.assertIn("do not claim", self.shown())

    def test_symlink_original_is_unavailable_even_for_matching_bytes(self):
        self.send([wire()])
        original = self.original()
        target = self.root / "foreign-original.txt"
        target.write_bytes(original.read_bytes())
        original.unlink()
        guard = None
        try:
            os.symlink(target, original)
        except OSError:
            # Stock Windows may deny symlink creation. Exercise the same lstat
            # rejection branch without requiring machine policy/admin changes.
            original.write_bytes(target.read_bytes())
            inspect = Path.lstat
            status = original.lstat()
            symlink_status = os.stat_result((stat.S_IFLNK, *tuple(status)[1:]))
            guard = mock.patch.object(Path, "lstat", autospec=True,
                side_effect=lambda path: symlink_status if path == original else inspect(path))
        with guard if guard is not None else mock.patch.object(context, "MAX_FILES", context.MAX_FILES):
            result = self.send()
        self.assertEqual(self.requests[-1].attachments, [])
        self.assertEqual(result["attachment_context"]["omitted_files"], 1)

    def test_current_inputs_have_priority_without_a_chat_lifetime_cap(self):
        self.send([wire("old.txt", b"OLDER-CONTENT")])
        with mock.patch.object(context, "MAX_BYTES", 20):
            result = self.send([wire("new.txt", b"NEWER-CONTENT")])
        self.assertEqual([one["name"] for one in self.requests[-1].attachments], ["new.txt"])
        self.assertEqual(result["attachment_context"]["omitted_files"], 1)
        self.assertTrue(self.original().exists())
        self.send()
        self.assertEqual(len(self.requests[-1].attachments), 2)

    def test_large_current_input_remains_valid_but_history_bundle_is_not_sliced(self):
        self.send([wire("large.txt", b"z" * (chat.CHAT_HISTORY_PROMPT_CHARACTERS + 100))])
        self.assertIn("z" * 1000, self.requests[-1].dynamic_context)
        result = self.send()
        self.assertEqual(self.requests[-1].attachments, [])
        self.assertNotIn("z" * 1000, self.shown())
        self.assertEqual(result["attachment_context"]["omitted_files"], 1)
        self.assertLessEqual(sum(len(one["content"]) for one in self.requests[-1].messages[:-1]), chat.CHAT_HISTORY_PROMPT_CHARACTERS)

    def test_question_metadata_counts_toward_rendered_history_budget(self):
        self.send([wire()])
        turns = chat.read_it(self.config, "", "arbitrary-shared-chat")
        turns[-1].questions = [{"id": "q", "prompt": "q" * 1000, "options": []}]
        chat._keep_it(self.config, "", turns, "arbitrary-shared-chat", replace_projection=True)
        with mock.patch.object(chat, "CHAT_HISTORY_PROMPT_CHARACTERS", 800):
            self.send()
        self.assertLessEqual(sum(len(one["content"]) for one in self.requests[-1].messages[:-1]), 800)
        self.assertNotIn("q" * 1000, self.shown())

    def test_exact_byte_boundary_and_whole_bundle_descriptor_limit(self):
        self.send([wire("one.txt", b"a" * 10), wire("two.txt", b"b" * 10)])
        with mock.patch.object(context, "MAX_BYTES", 20):
            accepted = self.send()
        self.assertEqual(len(self.requests[-1].attachments), 2)
        self.assertEqual(accepted["attachment_context"]["omitted_files"], 0)
        with mock.patch.object(context, "MAX_FILES", 1):
            omitted = self.send()
        self.assertEqual(self.requests[-1].attachments, [])
        self.assertEqual(omitted["attachment_context"]["omitted_files"], 2)

    def test_history_window_discloses_retained_files_without_hydrating_them(self):
        self.send([wire()])
        self.send()
        with mock.patch.object(chat, "MOST_KEPT", 2), mock.patch.object(context, "_original") as read:
            result = self.send()
            read.assert_not_called()
        self.assertEqual(result["attachment_context"]["reasons"], {"history_window": 1})
        self.assertTrue(self.original().exists())

    def test_invalid_current_upload_and_provider_failure_keep_transcript_unchanged(self):
        self.send([wire()])
        before = [one.to_dict() for one in chat.read_it(self.config, "", "arbitrary-shared-chat")]
        with self.assertRaises(chat.ChatError):
            self.send([wire("fake.png", b"not a PNG", "image/png")])
        with mock.patch.object(self.provider, "complete", side_effect=HarnessError("fixture provider unavailable")):
            with self.assertRaises(chat.ChatError):
                self.send()
        self.assertEqual(before, [one.to_dict() for one in chat.read_it(self.config, "", "arbitrary-shared-chat")])

    def test_persisted_notice_allowlist_does_not_copy_private_fields(self):
        self.send([wire()])
        self.original().unlink()
        result = self.send()
        raw = {**result["attachment_context"], "data": "private", "path": "private"}
        sanitized = chat._said_correlation({"schema_version": 1, "attachment_context": raw})
        self.assertNotIn("private", json.dumps(sanitized))


if __name__ == "__main__":
    unittest.main()
