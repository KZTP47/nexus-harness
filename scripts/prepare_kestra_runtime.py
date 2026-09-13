"""Prepare a checksummed, private Windows Kestra + Java distribution.

The Python runtime publication is deliberately independent. Binaries live in
desktop/kestra-runtime, excluded by that directory's tracked .gitignore.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "desktop" / "kestra-runtime.lock.json"
DEFAULT_OUTPUT = ROOT / "desktop" / "kestra-runtime"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root: Path) -> dict[str, str]:
    files = {}
    for p in sorted(root.rglob("*")):
        first = p.relative_to(root).parts[0]
        if first not in {"java", "kestra.jar"} and not first.startswith("KESTRA-LICENSE-"):
            continue
        if p.is_symlink():
            raise RuntimeError("Private Kestra distribution cannot contain symbolic links")
        if p.is_file():
            files[str(p.relative_to(root)).replace("\\", "/")] = digest(p)
    return files


def verify(root: Path) -> bool:
    try:
        manifest = json.loads((root / "NEXUS_KESTRA.json").read_text())
        lock = json.loads(LOCK.read_text())
        return (manifest["schema_version"] == 1
                and manifest["lock_sha256"] == digest(LOCK)
                and manifest["files"] == inventory(root)
                and (root / "java" / "bin" / "java.exe").is_file()
                and digest(root / "kestra.jar") == lock["kestra"]["sha256"])
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def download(url: str, target: Path, sha256: str) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "Nexus-Harness-build"})
    with urllib.request.urlopen(request, timeout=120) as response, target.open("wb") as out:
        shutil.copyfileobj(response, out)
    if digest(target) != sha256:
        raise RuntimeError(f"Downloaded runtime checksum mismatch: {target.name}")


def prepare(output: Path = DEFAULT_OUTPUT) -> Path:
    output = output.resolve()
    if verify(output):
        return output
    lock = json.loads(LOCK.read_text())
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".kestra-stage-", dir=output.parent) as stage:
        stage_root = Path(stage)
        payload = stage_root / "payload"
        payload.mkdir()
        download(lock["kestra"]["url"], payload / "kestra.jar", lock["kestra"]["sha256"])
        archive = stage_root / "java.zip"
        download(lock["java"]["url"], archive, lock["java"]["sha256"])
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                relative = Path(member.filename)
                if relative.is_absolute() or relative.drive or ".." in relative.parts:
                    raise RuntimeError("Unsafe path in Java distribution")
            z.extractall(stage_root / "unpack")
        roots = list((stage_root / "unpack").iterdir())
        if len(roots) != 1 or not (roots[0] / "bin" / "java.exe").is_file():
            raise RuntimeError("Unexpected private Java archive structure")
        shutil.move(str(roots[0]), payload / "java")
        # License payloads remain inside the Java distribution and Kestra JAR.
        with zipfile.ZipFile(payload / "kestra.jar") as z:
            licenses = [n for n in z.namelist() if n.upper() in {"META-INF/LICENSE", "META-INF/LICENSE.TXT", "LICENSE"}]
            for i, name in enumerate(licenses):
                (payload / f"KESTRA-LICENSE-{i}.txt").write_bytes(z.read(name))
        (payload / "NEXUS_KESTRA.json").write_text(json.dumps({
            "schema_version": 1, "lock_sha256": digest(LOCK),
            "kestra": lock["kestra"]["version"], "java": lock["java"]["version"],
            "files": inventory(payload),
        }, indent=2) + "\n")
        output.mkdir(exist_ok=True)
        # Replace only generated owned files. Diagnostics and unrelated files
        # remain untouched and are excluded by the packager's explicit allowlist.
        for item in output.iterdir():
            generated = item.name in {"java", "kestra.jar", "NEXUS_KESTRA.json"} or (
                item.name.startswith("KESTRA-LICENSE-") and item.suffix == ".txt")
            if not generated:
                continue
            if item.is_symlink() or item.resolve().parent != output:
                raise RuntimeError("Generated Kestra replacement escapes its owned output root")
            if item.is_dir():
                if item.name != "java":
                    raise RuntimeError("Unexpected generated runtime directory")
                shutil.rmtree(item)
            else:
                item.unlink()
        for item in payload.iterdir():
            shutil.move(str(item), output / item.name)
    if not verify(output):
        raise RuntimeError("Prepared Kestra distribution failed verification")
    return output


if __name__ == "__main__":
    print(prepare())
