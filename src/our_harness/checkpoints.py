from __future__ import annotations

import hashlib
import json
import stat
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from .changes import FileTransaction, file_sha256
from .config import LoadedConfig
from .models import ChangePlan, HarnessError
from .safety import confined_path, confined_walk_files


class CheckpointManager:
    def __init__(self, config: LoadedConfig):
        self.config = config
        self.root = config.project_root
        self.folder = confined_path(self.root, ".harness/checkpoints", allow_control=True)
        self.ignore = set(config.get("project.ignore", [])) | {".harness", ".git"}

    def create(self, note: str = "") -> dict[str, Any]:
        folder = confined_path(self.root, ".harness/checkpoints", allow_control=True)
        folder.mkdir(parents=True, exist_ok=True)
        folder = confined_path(self.root, ".harness/checkpoints", allow_missing=False, allow_control=True)
        checkpoint_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        path = folder / f"{checkpoint_id}.zip"
        temporary = path.with_suffix(".zip.tmp")
        manifest: dict[str, Any] = {"schema_version": 2, "id": checkpoint_id, "note": note, "created_at": int(time.time()), "files": []}
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for discovered in confined_walk_files(self.root, self.ignore):
                    relative = discovered.relative_to(self.root)
                    source = confined_path(self.root, relative, allow_missing=False)
                    metadata = source.stat(follow_symlinks=False)
                    raw = source.read_bytes()
                    name = relative.as_posix()
                    archive.writestr(f"files/{name}", raw)
                    manifest["files"].append(
                        {
                            "path": name,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                            "bytes": len(raw),
                            "mode": stat.S_IMODE(metadata.st_mode),
                        }
                    )
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return {**manifest, "archive": str(path)}

    def list(self) -> list[dict[str, Any]]:
        folder = confined_path(self.root, ".harness/checkpoints", allow_control=True)
        if not folder.exists():
            return []
        folder = confined_path(self.root, ".harness/checkpoints", allow_missing=False, allow_control=True)
        output = []
        paths = [path for path in confined_walk_files(folder) if path.parent == folder and path.suffix == ".zip"]
        for path in sorted(paths, reverse=True):
            try:
                with zipfile.ZipFile(path) as archive:
                    manifest = json.loads(archive.read("manifest.json"))
                output.append({**manifest, "archive": str(path)})
            except (OSError, zipfile.BadZipFile, KeyError, json.JSONDecodeError):
                output.append({"id": path.stem, "archive": str(path), "error": "invalid checkpoint"})
        return output

    def restore_file(self, checkpoint_id: str, relative: str) -> dict[str, Any]:
        archive_path = confined_path(
            self.root,
            Path(".harness/checkpoints") / f"{checkpoint_id}.zip",
            allow_missing=False,
            allow_control=True,
        )
        target = confined_path(self.root, relative)
        normalized = Path(relative).as_posix()
        member = f"files/{normalized}"
        try:
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                records = [item for item in manifest.get("files", []) if isinstance(item, dict) and item.get("path") == normalized]
                if len(records) != 1:
                    raise HarnessError(f"Checkpoint manifest does not contain one record for: {relative}")
                record = records[0]
                info = archive.getinfo(member)
                if info.file_size > int(self.config.get("execution.max_changed_bytes")):
                    raise HarnessError("Checkpoint file exceeds the configured change-byte limit")
                content = archive.read(info)
                if len(content) != record.get("bytes") or hashlib.sha256(content).hexdigest() != record.get("sha256"):
                    raise HarnessError(f"Checkpoint file does not match its manifest: {relative}")
                mode = record.get("mode")
                if mode is None and int(manifest.get("schema_version", 1)) < 2:
                    mode = stat.S_IMODE(target.stat(follow_symlinks=False).st_mode) if target.is_file() else None
                elif isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o7777:
                    raise HarnessError(f"Checkpoint file mode is invalid: {relative}")
        except KeyError as exc:
            raise HarnessError(f"Checkpoint does not contain: {relative}") from exc
        except (zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise HarnessError(f"Checkpoint is invalid: {checkpoint_id}") from exc
        transaction = FileTransaction(self.root, 1, int(self.config.get("execution.max_changed_bytes")))
        return transaction.apply(
            [ChangePlan(relative, file_sha256(target), content, reason=f"restore from checkpoint {checkpoint_id}", mode=mode)]
        )
