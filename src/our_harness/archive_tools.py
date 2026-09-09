"""Read ZIPs and extract inspection copies without touching project deliverables."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import zipfile
import zlib

from .models import HarnessError
from .safety import confined_path, portable_relative_path_key

ARCHIVE_VERSION = 1
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2000


class ZipInspection:
    def __init__(self, raw: bytes):
        if len(raw) > MAX_ARCHIVE_BYTES:
            raise HarnessError("ZIP exceeds the 8 MB archive limit")
        self.sha256 = hashlib.sha256(raw).hexdigest()
        try:
            self.archive = zipfile.ZipFile(io.BytesIO(raw))
            self.entries = self.archive.infolist()
            if len(self.entries) > MAX_ARCHIVE_ENTRIES:
                raise HarnessError("ZIP contains too many entries")
            if sum(info.file_size for info in self.entries) > MAX_EXPANDED_BYTES:
                raise HarnessError("ZIP expanded size exceeds 32 MB")
            keys: dict[str, bool] = {}
            for info in self.entries:
                name = info.filename.rstrip("/")
                if "\\" in name or info.orig_filename != info.filename or name in {"", "."}:
                    raise HarnessError("ZIP contains an ambiguous member path")
                key = portable_relative_path_key(name, allow_control=True)
                if key in keys:
                    raise HarnessError("ZIP contains duplicate or case-alias member paths")
                mode = stat.S_IFMT(info.external_attr >> 16)
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    raise HarnessError("ZIP links and special files are not supported")
                if info.flag_bits & 1:
                    raise HarnessError("ZIP is encrypted; provide an unencrypted copy")
                keys[key] = info.is_dir()
            for key in keys:
                parent = key.rpartition("/")[0]
                while parent:
                    if parent in keys and not keys[parent]:
                        raise HarnessError("ZIP contains conflicting file and directory paths")
                    parent = parent.rpartition("/")[0]
        except (zipfile.BadZipFile, ValueError, OSError) as exc:
            raise HarnessError("Cannot open ZIP: archive is damaged or unsupported") from exc

    def close(self):
        self.archive.close()

    def read(self, member: str) -> bytes:
        try:
            info = self.archive.getinfo(member)
            if info.is_dir():
                raise HarnessError("ZIP member is a directory; use list_archive")
            with self.archive.open(info) as stream:
                raw = stream.read(MAX_EXPANDED_BYTES + 1)
            if len(raw) > MAX_EXPANDED_BYTES:
                raise HarnessError("ZIP member exceeds the expanded size limit")
            return raw
        except (KeyError, zipfile.BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError, zlib.error) as exc:
            raise HarnessError("Cannot read ZIP member: missing, encrypted, damaged or unsupported") from exc

    def listing(self) -> list[dict]:
        return [{"member": one.filename, "bytes": one.file_size, "directory": one.is_dir()} for one in self.entries]

    def extract(self, root: Path) -> dict:
        parent = confined_path(root, ".harness/archive-inspection", allow_control=True, allow_missing=True)
        parent.mkdir(parents=True, exist_ok=True)
        destination = Path(tempfile.mkdtemp(prefix=self.sha256[:16] + "-", dir=parent))
        try:
            files = []
            # The archive cannot choose the inspection root or overwrite existing files.
            for info in self.entries:
                target = confined_path(destination, info.filename.rstrip("/"), allow_control=True, allow_missing=True)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                raw = self.read(info.filename)
                with target.open("xb") as stream:
                    stream.write(raw)
                files.append({"member": info.filename, "sha256": hashlib.sha256(raw).hexdigest()})
            manifest = {"schema_version": ARCHIVE_VERSION, "source_sha256": self.sha256, "files": files,
                        "contract_fingerprint_sha256": hashlib.sha256(json.dumps([
                            ARCHIVE_VERSION, os.path.normcase(str(root.resolve())), MAX_EXPANDED_BYTES, MAX_ARCHIVE_ENTRIES
                        ]).encode()).hexdigest()}
            # Store the receipt beside the extracted tree, outside the archive namespace.
            destination.with_suffix(".manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            return {"extracted_to": str(destination), "source_sha256": self.sha256, "files": len(files),
                    "schema_version": ARCHIVE_VERSION, "contract_fingerprint_sha256": manifest["contract_fingerprint_sha256"],
                    "note": "Inspection copy only. Use list_archive/read_archive with the original path to inspect; propose normal project changes to import files. Nothing was executed."}
        except BaseException:
            if destination.resolve().parent == parent.resolve():
                shutil.rmtree(destination)
            raise
