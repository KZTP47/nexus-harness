from __future__ import annotations

import ast
import hashlib
import ipaddress
import math
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from .config import LoadedConfig
from .ignore_policy import IgnorePolicy
from .memory import MemoryStore, bounded_chunks
from .models import Deadline
from .safety import confined_path


TEXT_EXTENSIONS = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".go": "go", ".rs": "rust",
    ".java": "java", ".cs": "csharp", ".cpp": "cpp", ".cc": "cpp", ".c": "c",
    ".h": "cpp", ".hpp": "cpp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".kt": "kotlin", ".kts": "kotlin", ".json": "json", ".toml": "toml",
    ".yaml": "yaml", ".yml": "yaml", ".md": "markdown", ".sh": "shell", ".ps1": "powershell",
}

IMPORT_PATTERNS = [
    re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE),
    re.compile(r"(?:require\(|from\s+)[\"']([^\"']+)[\"']"),
    re.compile(r"^\s*use\s+([\w:]+)", re.MULTILINE),
    re.compile(r"^\s*#include\s*[<\"]([^>\"]+)[>\"]", re.MULTILINE),
]

SYMBOL_PATTERNS = [
    ("class", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:def|function|fn)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)),
    ("function", re.compile(r"^\s*(?!(?:if|for|while|switch|return)\b)(?:public\s+|private\s+|protected\s+|static\s+)*[\w:<>,\[\]?]+\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)),
]

class WorkspaceIndexer:
    def __init__(
        self,
        config: LoadedConfig,
        memory: MemoryStore,
        embedder: Callable[..., list[list[float]]] | None = None,
    ):
        self.config = config
        self.memory = memory
        self.root = config.project_root
        self.ignore = set(config.get("project.ignore", []))
        self.max_bytes = int(config.get("project.max_file_bytes"))
        self.embedder = embedder

    def _embedding_route(self, enabled: bool) -> str:
        if not enabled:
            return "disabled"
        requested = str(self.config.get("memory.embedding_provider") or self.config.get("provider.name"))
        if requested == str(self.config.get("provider.name")):
            endpoint = str(self.config.get("provider.endpoint") or "")
        else:
            endpoint = {
                "openai": "https://api.openai.com/v1",
                "ollama": "http://127.0.0.1:11434",
                "openai-compatible": "http://127.0.0.1:8000/v1",
            }.get(requested, "https://invalid.invalid")
        try:
            host = urlsplit(endpoint).hostname
            if host == "localhost" or (host and ipaddress.ip_address(host).is_loopback):
                return "local"
        except ValueError:
            pass
        return "remote"

    @staticmethod
    def _embedding_text(path: str, content: str, limit: int = 16_000) -> str:
        prefix = f"FILE {path}\n"
        available = max(0, limit - len(prefix))
        if len(content) <= available:
            return prefix + content
        head = int(available * 0.7)
        tail = max(0, available - head - 40)
        return prefix + content[:head] + "\n[content shortened]\n" + content[-tail:]

    @staticmethod
    def _valid_vector(vector: object) -> bool:
        return (
            isinstance(vector, list)
            and bool(vector)
            and all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in vector)
        )

    @staticmethod
    def _fallback_intelligence(path: str, text: str) -> tuple[list[tuple[str, str]], list[dict[str, object]]]:
        edges: list[tuple[str, str]] = []
        for pattern in IMPORT_PATTERNS:
            for match in pattern.finditer(text):
                target = next((value for value in match.groups() if value), "")
                if target:
                    edges.append((path, target))
        symbols: list[dict[str, object]] = []
        for kind, pattern in SYMBOL_PATTERNS:
            for match in pattern.finditer(text):
                name = match.group(1)
                line = text.count("\n", 0, match.start()) + 1
                symbols.append({"name": name, "qualified_name": name, "kind": kind, "line": line, "end_line": line})
        return sorted(set(edges)), sorted(symbols, key=lambda item: (int(item["line"]), str(item["qualified_name"]), str(item["kind"])))

    @classmethod
    def _python_intelligence(cls, path: str, text: str) -> tuple[list[tuple[str, str]], list[dict[str, object]]]:
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            return cls._fallback_intelligence(path, text)
        edges: list[tuple[str, str]] = []
        symbols: list[dict[str, object]] = []

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.scope: list[str] = []
                self.scope_kinds: list[str] = []

            def _symbol(self, node: ast.AST, name: str, kind: str) -> None:
                qualified = ".".join([*self.scope, name])
                symbols.append(
                    {
                        "name": name,
                        "qualified_name": qualified,
                        "kind": kind,
                        "line": int(getattr(node, "lineno", 1)),
                        "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
                    }
                )

            def visit_Import(self, node: ast.Import) -> None:
                for alias in node.names:
                    edges.append((path, alias.name))
                    self._symbol(node, alias.asname or alias.name.split(".")[0], "import")

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                package = path.replace("\\", "/").split("/")[:-1]
                if node.level:
                    keep = max(0, len(package) - (node.level - 1))
                    module_parts = [*package[:keep], *((node.module or "").split(".") if node.module else [])]
                    module = ".".join(part for part in module_parts if part)
                else:
                    module = node.module or ""
                if module:
                    edges.append((path, module))
                for alias in node.names:
                    if alias.name != "*":
                        if node.level and not node.module and module:
                            edges.append((path, f"{module}.{alias.name}"))
                        self._symbol(node, alias.asname or alias.name, "import")

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self._symbol(node, node.name, "class")
                self.scope.append(node.name)
                self.scope_kinds.append("class")
                self.generic_visit(node)
                self.scope_kinds.pop()
                self.scope.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                self._symbol(node, node.name, "method" if self.scope_kinds and self.scope_kinds[-1] == "class" else "function")
                self.scope.append(node.name)
                self.scope_kinds.append("function")
                self.generic_visit(node)
                self.scope_kinds.pop()
                self.scope.pop()

        Visitor().visit(tree)
        return sorted(set(edges)), sorted(symbols, key=lambda item: (int(item["line"]), str(item["qualified_name"]), str(item["kind"])))

    @classmethod
    def _intelligence(cls, path: str, language: str, text: str) -> tuple[list[tuple[str, str]], list[dict[str, object]]]:
        return cls._python_intelligence(path, text) if language == "python" else cls._fallback_intelligence(path, text)

    def scan(self, deadline: Deadline | None = None) -> dict[str, Any]:
        if not self.memory.enabled:
            return {"files": 0, "updated": 0, "skipped": 0, "embedded": 0, "embedding_errors": 0,
                    "embedding_route": "disabled", "embedding_selected_files": []}
        if deadline is not None:
            deadline.check("before workspace indexing")
        seen: set[str] = set()
        updated = 0
        skipped = 0
        pending: list[dict[str, object]] = []
        wants_embeddings = bool(self.config.get("memory.embedding_model"))
        policy = IgnorePolicy(self.root, self.ignore)
        for discovered in policy.walk_files():
            if deadline is not None:
                deadline.check("during workspace file discovery")
            relative = discovered.relative_to(self.root)
            path = confined_path(self.root, relative, allow_missing=False)
            name = relative.as_posix()
            if path.suffix.lower() not in TEXT_EXTENSIONS:
                continue
            metadata = path.stat(follow_symlinks=False)
            if metadata.st_size > self.max_bytes:
                skipped += 1
                continue
            seen.add(name)
            raw = path.read_bytes()
            if deadline is not None:
                deadline.check("after reading a workspace file")
            digest = hashlib.sha256(raw).hexdigest()
            content_changed = self.memory.document_hash(name) != digest
            needs_embedding = wants_embeddings and self.memory.document_needs_embedding(name)
            if not content_changed and not needs_embedding:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                skipped += 1
                continue
            language = TEXT_EXTENSIONS[path.suffix.lower()]
            edges, symbols = self._intelligence(name, language, text)
            safe_text = self.memory.redact_text(text)
            chunks = bounded_chunks(safe_text)
            for chunk in chunks:
                chunk["symbols"] = [
                    str(symbol["qualified_name"])
                    for symbol in symbols
                    if int(symbol["line"]) <= int(chunk["end_line"])
                    and int(symbol["end_line"]) >= int(chunk["start_line"])
                ]
            pending.append(
                {
                    "name": name,
                    "digest": digest,
                    "language": language,
                    "text": safe_text,
                    "edges": edges,
                    "symbols": symbols,
                    "chunks": chunks,
                }
            )
            if content_changed:
                updated += 1
        embedded = 0
        embedding_errors = 0
        if wants_embeddings and pending:
            embedder = self.embedder
            if embedder is None:
                try:
                    from .providers import create_embedding_provider

                    embedder = create_embedding_provider(self.config).embed
                except Exception:
                    embedder = None
            embedding_items = [
                (item, chunk)
                for item in pending
                for chunk in item["chunks"]  # type: ignore[union-attr]
            ]
            for offset in range(0, len(embedding_items), 16):
                if deadline is not None:
                    deadline.check("before a workspace embedding batch")
                batch = embedding_items[offset : offset + 16]
                vectors: list[list[float]] = []
                if embedder is not None:
                    try:
                        texts = [self._embedding_text(str(item[0]["name"]), str(item[1]["content"])) for item in batch]
                        if deadline is None:
                            vectors = embedder(texts)
                        else:
                            timeout = deadline.remaining_seconds(
                                "before a workspace embedding provider call",
                                float(self.config.get("provider.timeout_seconds")),
                            )
                            vectors = embedder(texts, timeout_seconds=timeout)
                            deadline.check("after a workspace embedding provider call")
                    except Exception:
                        if deadline is not None:
                            deadline.check("after a workspace embedding provider call")
                        vectors = []
                if len(vectors) != len(batch) or any(not self._valid_vector(vector) for vector in vectors):
                    vectors = [[] for _ in batch]
                    embedding_errors += len(batch)
                for (_, chunk), vector in zip(batch, vectors):
                    if deadline is not None:
                        deadline.check("while preparing workspace embeddings")
                    valid = [float(value) for value in vector] if vector else None
                    chunk["embedding"] = valid
                    embedded += int(valid is not None)
        for item in pending:
            if deadline is not None:
                deadline.check("while storing workspace documents")
            self.memory.upsert_document(
                str(item["name"]),
                str(item["digest"]),
                str(item["language"]),
                str(item["text"]),
                item["edges"],  # type: ignore[arg-type]
                chunks=item["chunks"],  # type: ignore[arg-type]
                symbols=item["symbols"],  # type: ignore[arg-type]
            )
        if deadline is not None:
            deadline.check("before pruning the workspace index")
        self.memory.remove_documents_not_in(seen)
        return {
            "files": len(seen),
            "updated": updated,
            "skipped": skipped,
            "embedded": embedded,
            "embedding_errors": embedding_errors,
            "embedding_route": self._embedding_route(wants_embeddings),
            "embedding_selected_files": sorted(str(item["name"]) for item in pending) if wants_embeddings else [],
        }
