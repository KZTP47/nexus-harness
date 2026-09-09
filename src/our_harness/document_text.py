"""Portable, bounded DOCX text extraction; no Office install or external tools."""

from __future__ import annotations

import io
from pathlib import PurePosixPath
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib

from .models import HarnessError


DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCX_TEXT_VERSION = 1
MAX_DOCX_XML_BYTES = 32 * 1024 * 1024
MAX_DOCX_MEMBERS = 2048
MAX_DOCX_TEXT = 2_000_000
_WORD_NAMESPACES = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "http://purl.oclc.org/ooxml/wordprocessingml/main",
}
_EXTRA_PART = re.compile(r"word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml\Z")


def is_docx(name: str, mime: str = "") -> bool:
    return PurePosixPath(name.replace("\\", "/")).suffix.lower() == ".docx" or mime == DOCX_MIME


class _DocumentTree(ET.TreeBuilder):
    def __init__(self):
        super().__init__()
        self.depth = 0

    def doctype(self, name, pubid, system):
        raise HarnessError("DOCX XML must not contain a document type or entity declarations")

    def start(self, tag, attrs):
        self.depth += 1
        if self.depth > 128:
            raise HarnessError("DOCX XML nesting exceeds the supported limit")
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)


def _word_tag(element: ET.Element) -> str:
    namespace, _, local = element.tag.rpartition("}")
    return local if namespace.lstrip("{") in _WORD_NAMESPACES else ""


def _render(element: ET.Element) -> str:
    tag = _word_tag(element)
    if tag in {"del", "moveFrom", "instrText"}:
        return ""
    if tag == "t":
        return element.text or ""
    if tag in {"tab", "ptab"}:
        return "\t"
    if tag in {"br", "cr"}:
        return "\n"
    if tag == "noBreakHyphen":
        return "\u2011"
    if tag == "softHyphen":
        return "\u00ad"
    if tag == "tr":
        return "\t".join(_render(child).rstrip("\n") for child in element if _word_tag(child) == "tc") + "\n"
    content = "".join(_render(child) for child in element)
    return content + "\n" if tag == "p" else content


def extract_docx_text(raw: bytes) -> str:
    """Read body, tables, headers/footers and notes without unpacking to disk.

    This is text extraction, not page rendering or OCR. Archive members and
    decompressed XML are bounded before parsing; links are never followed.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > MAX_DOCX_MEMBERS:
                raise HarnessError("DOCX contains too many archive entries")
            names = [member.filename for member in members]
            if len(set(names)) != len(names):
                raise HarnessError("DOCX contains ambiguous duplicate archive entries")
            if "word/document.xml" not in names:
                raise HarnessError("DOCX is missing its Word document text")
            parts = ["word/document.xml"] + sorted(name for name in names if _EXTRA_PART.fullmatch(name))
            if sum(archive.getinfo(name).file_size for name in parts) > MAX_DOCX_XML_BYTES:
                raise HarnessError("DOCX expanded text exceeds the supported size limit")
            blocks = []
            text_size = 0
            for name in parts:
                with archive.open(name) as stream:
                    xml = stream.read(MAX_DOCX_XML_BYTES + 1)
                if len(xml) > MAX_DOCX_XML_BYTES:
                    raise HarnessError("DOCX expanded text exceeds the supported size limit")
                root = ET.fromstring(xml, parser=ET.XMLParser(target=_DocumentTree()))
                expected = "document" if name == "word/document.xml" else None
                if expected and _word_tag(root) != expected:
                    raise HarnessError("DOCX has an invalid Word document root")
                content = _render(root).strip("\n")
                if content.strip():
                    block = content if expected else f"[{PurePosixPath(name).stem}]\n{content}"
                    text_size += len(block) + 2
                    if text_size > MAX_DOCX_TEXT:
                        raise HarnessError("DOCX extracted text exceeds the supported size limit; split the document")
                    blocks.append(block)
    except HarnessError:
        raise
    except (zipfile.BadZipFile, ET.ParseError, RuntimeError, NotImplementedError, OSError, ValueError, EOFError, zlib.error) as exc:
        raise HarnessError("Cannot read this DOCX. It may be damaged or encrypted; save an unencrypted .docx copy in Word or LibreOffice.") from exc
    if not blocks:
        raise HarnessError("This DOCX contains no extractable text. Image-only documents need OCR or the original text.")
    return "\n\n".join(blocks) + "\n"
