"""Durable, pair-scoped conversations for the board of agents.

The ordinary chat transcript store deliberately knows only a provider route
and a safe file name.  This module adds the missing board-level identity: one
conversation belongs to one canonical pair of agents and selects one project
that both agents work on.  Every server action resolves that metadata again,
so a stale or hand-edited browser request cannot silently change the pair or
the folder that receives file changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import chat as chat_lab
from . import swarm as swarm_lab
from . import pipeline_runs
from .config import LoadedConfig
from .models import HarnessError
from .providers.base import effective_dispatch_fingerprint
from .redaction import CredentialRedactor


WHERE_THEY_LIVE = ".harness/chats/_board-conversations.json"
BACKUP_NAME = "_board-conversations.last-good.json"
HISTORY_FOLDER = "_board-conversation-history"
MOST_CHATS = 240
MOST_PER_PAIR = 40
REGISTRY_SCHEMA_VERSION = 7
REGISTRY_INTEGRITY_VERSION = 1
MIGRATABLE_REGISTRY_SCHEMAS = frozenset({1, 2, 3, 4, 5, 6})
CHAT_BINDING_SCHEMA_VERSION = 3
MIGRATABLE_CHAT_BINDING_SCHEMAS = frozenset({1, 2})
LEGACY_PATH_ONLY_CHAT_BINDING_SCHEMAS = frozenset({1})
LEGACY_EFFECTIVE_DISPATCH_SCHEMAS = frozenset({1, 2})
PROJECT_DIRECTORY_IDENTITY_VERSION = 1
LEGACY_WEB_RELAY_CONTRACT = "web-chat/electron-relay/v1"
CURRENT_WEB_RELAY_CONTRACT = "web-chat/electron-relay/v2"
WEB_EFFECTIVE_DISPATCH_CONTRACT = "web-chat/effective-dispatch/v1"
_CHAT_ID = re.compile(r"^chat-[0-9a-f]{16}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_WEB_CONVERSATION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_lock = threading.Lock()


@contextmanager
def _registry_transaction(
    config: LoadedConfig, timeout_seconds: float = 30.0,
) -> Iterator[None]:
    """Serialize one whole-registry read/modify/write across Nexus processes.

    Provider work and transcript commits retain their per-chat locks. This
    intentionally short, global boundary covers only metadata stored in the
    single JSON registry, whose atomic replace prevents torn files but cannot
    by itself prevent two processes from overwriting each other's sibling-chat
    updates.
    """

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    if not _lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise swarm_lab.SwarmError(
            "Saved chat metadata is busy in another Nexus window. Retry shortly."
        )
    stream = None
    acquired = False
    try:
        where = _where(config)
        where.parent.mkdir(parents=True, exist_ok=True)
        # Validate the lock target independently from the JSON registry. A
        # pre-existing symlink/reparse at only the lock name must not redirect
        # the initialization byte or advisory lock outside this project.
        from .safety import confined_path

        lock_path = confined_path(
            config.project_root,
            ".harness/chats/_board-conversations.lock",
            allow_missing=True,
            allow_control=True,
        )
        stream = lock_path.open("a+b")
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
            os.fsync(stream.fileno())
        while not acquired:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise swarm_lab.SwarmError(
                        "Saved chat metadata is busy in another Nexus window. Retry shortly."
                    ) from exc
                time.sleep(0.05)
        yield
    finally:
        try:
            if stream is not None:
                try:
                    if acquired:
                        stream.seek(0)
                        if os.name == "nt":
                            import msvcrt

                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
        finally:
            _lock.release()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _where(config: LoadedConfig) -> Path:
    return _artifact_path(config, Path(WHERE_THEY_LIVE))


def _artifact_path(config: LoadedConfig, relative: Path) -> Path:
    from .safety import confined_path

    return confined_path(
        config.project_root, relative,
        allow_missing=True, allow_control=True,
    )


def _backup_where(config: LoadedConfig) -> Path:
    return _artifact_path(config, Path(".harness/chats") / BACKUP_NAME)


def _history_where(config: LoadedConfig) -> Path:
    return _artifact_path(config, Path(".harness/chats") / HISTORY_FOLDER)


def _history_snapshot_where(config: LoadedConfig, filename: str) -> Path:
    return _artifact_path(
        config, Path(".harness/chats") / HISTORY_FOLDER / filename,
    )


def _empty() -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "revision": 0,
        "chats": [],
        "active": {},
        "chosen_active": {},
        "known_pairs": [],
    }


def _board_workspace_id(board: dict[str, Any]) -> str:
    """Return the server-owned board scope, with a test/legacy bridge."""

    held = str(board.get("workspace_id") or "").strip().lower()
    if held:
        if swarm_lab._WORKSPACE_ID.fullmatch(held):  # noqa: SLF001 - shared schema
            return held
        raise swarm_lab.SwarmError(
            "This board has an invalid workspace identity. Nexus did not claim or "
            "retarget any saved chats."
        )
    # Product board reads always assign an id. Direct library callers and old
    # focused tests may still provide a pre-identity board dictionary; keep
    # those calls deterministic without granting a portable/imported identity.
    return "workspace-legacy-000000000000000000000000"


def _may_adopt_legacy(workspace_id: str) -> bool:
    return str(workspace_id).startswith("workspace-legacy-")


def _scoped_key(workspace_id: str, value: str) -> str:
    return f"{workspace_id}:{value}"


def _pair_scope_key(workspace_id: str, pair: list[str]) -> str:
    return _scoped_key(workspace_id, _pair_key(pair))


def _read_object(where: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _registry_integrity(value: dict[str, Any]) -> str:
    """Digest one complete registry envelope without its digest field."""

    payload = {
        key: held for key, held in value.items()
        if key != "integrity_sha256" and not str(key).startswith("_")
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _registry_candidate(where: Path) -> dict[str, Any] | None:
    """Read one complete supported registry copy, or None when it is corrupt."""

    value = _read_object(where)
    if value is None:
        return None
    version = value.get("schema_version")
    if version is None:
        # The original pre-version registry is the one explicit pre-v1 bridge.
        effective_version = 1
    elif isinstance(version, bool) or not isinstance(version, int):
        raise swarm_lab.SwarmError(
            f"{where.name} has an invalid saved-chat schema version. Nexus did not rewrite it."
        )
    elif version == REGISTRY_SCHEMA_VERSION or version in MIGRATABLE_REGISTRY_SCHEMAS:
        effective_version = version
    else:
        qualifier = "newer" if version > REGISTRY_SCHEMA_VERSION else "unsupported"
        raise swarm_lab.SwarmError(
            f"{where.name} uses a {qualifier} saved-chat schema version ({version}). "
            "Nexus did not reinterpret or rewrite it."
        )

    revision = value.get("revision")
    if revision is None:
        revision = 0
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return None

    held_integrity = value.get("integrity_sha256")
    integrity_version = value.get("integrity_version")
    sealed = held_integrity is not None or integrity_version is not None
    if not sealed and revision != 0:
        # Revisions and integrity were introduced together. Accepting a
        # revisioned-but-unsealed current file would let a valid-looking
        # shortening delete its digest fields and outrank the complete backup.
        return None
    if sealed:
        if (
            isinstance(integrity_version, bool)
            or not isinstance(integrity_version, int)
            or integrity_version != REGISTRY_INTEGRITY_VERSION
        ):
            if (
                isinstance(integrity_version, int)
                and not isinstance(integrity_version, bool)
                and integrity_version > REGISTRY_INTEGRITY_VERSION
            ):
                raise swarm_lab.SwarmError(
                    f"{where.name} uses a newer saved-chat integrity contract. "
                    "Nexus did not reinterpret or rewrite it."
                )
            return None
        digest = str(held_integrity or "").lower()
        if not _SHA256.fullmatch(digest) or digest != _registry_integrity(value):
            return None

    return {
        "value": value,
        "revision": revision,
        "effective_schema_version": effective_version,
        "sealed": sealed,
    }


def _candidate_chat_ids(candidate: dict[str, Any]) -> set[str]:
    value = candidate["value"]
    return {
        str(one.get("id") or "")
        for one in value.get("chats", [])
        if isinstance(one, dict) and _CHAT_ID.fullmatch(str(one.get("id") or ""))
    } if isinstance(value.get("chats"), list) else set()


def _same_candidate(
    first: dict[str, Any], second: dict[str, Any],
) -> bool:
    return _registry_integrity(first["value"]) == _registry_integrity(second["value"])


def _choose_registry_copy(
    primary: dict[str, Any] | None,
    backup: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, bool]:
    """Choose the newest complete copy; bool says whether backup won."""

    if primary is None:
        return backup, backup is not None
    if backup is None:
        return primary, False
    first_revision = int(primary["revision"])
    second_revision = int(backup["revision"])
    if first_revision != second_revision:
        return (
            (primary, False)
            if first_revision > second_revision else
            (backup, True)
        )
    if _same_candidate(primary, backup):
        return primary, False
    if first_revision > 0:
        raise swarm_lab.SwarmError(
            "The saved-chat primary and backup disagree at the same revision. "
            "Nexus kept both and did not guess which conversations to expose."
        )
    # Pre-revision registries used append/archive semantics: chat identities
    # never disappeared during a legitimate write. If one legacy copy is a
    # strict subset, it is a valid-looking shortening and the superset wins.
    primary_ids = _candidate_chat_ids(primary)
    backup_ids = _candidate_chat_ids(backup)
    if primary_ids < backup_ids:
        return backup, True
    return primary, False


def _read(config: LoadedConfig) -> dict[str, Any]:
    where = _where(config)
    backup = _backup_where(config)
    selected, used_backup = _choose_registry_copy(
        _registry_candidate(where), _registry_candidate(backup),
    )
    recovered_from = ""
    if used_backup:
        recovered_from = backup.name
    if selected is None:
        history = _history_where(config)
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for candidate in sorted(
            history.glob("_board-conversations-*.json"), reverse=True
        ) if history.is_dir() else []:
            safe_candidate = _history_snapshot_where(config, candidate.name)
            found = _registry_candidate(safe_candidate)
            if found is not None:
                candidates.append((safe_candidate, found))
        if candidates:
            candidate, selected = max(
                candidates,
                key=lambda one: (
                    int(one[1]["revision"]), len(_candidate_chat_ids(one[1])),
                    one[0].name,
                ),
            )
            recovered_from = candidate.relative_to(where.parent).as_posix()
    if selected is None:
        return _empty()
    value = selected["value"]
    chats = []
    seen_ids: set[str] = set()
    needs_rewrite = (
        selected["effective_schema_version"] != REGISTRY_SCHEMA_VERSION
        or not bool(selected["sealed"])
        or bool(recovered_from)
    )
    for raw in value.get("chats", []) if isinstance(value.get("chats"), list) else []:
        if not isinstance(raw, dict) or not _CHAT_ID.fullmatch(str(raw.get("id") or "")):
            continue
        pair = raw.get("pair")
        if (
            not isinstance(pair, list) or len(pair) not in (1, 2)
            or any(not str(one or "").strip() for one in pair)
        ):
            continue
        canonical = sorted(dict.fromkeys(str(one)[:120] for one in pair))
        if len(canonical) != len(pair):
            continue
        chat_id = str(raw["id"])
        if chat_id in seen_ids:
            needs_rewrite = True
            continue
        seen_ids.add(chat_id)
        workspace_id = str(raw.get("workspace_id") or "").strip().lower()
        if workspace_id and not swarm_lab._WORKSPACE_ID.fullmatch(  # noqa: SLF001
            workspace_id
        ):
            needs_rewrite = True
            continue
        filed_as_version = raw.get("filed_as_version")
        if filed_as_version not in (1, 2):
            filed_as_version = 2 if workspace_id else 1
            needs_rewrite = True
        if filed_as_version == 2 and not workspace_id:
            filed_as_version = 1
            needs_rewrite = True
        canonical_filed_as = _filed_as(
            canonical, chat_id, workspace_id if filed_as_version == 2 else ""
        )
        if str(raw.get("filed_as") or "") != canonical_filed_as:
            needs_rewrite = True
        web_conversation_key = str(
            raw.get("web_conversation_key") or canonical_filed_as
        )
        if not _WEB_CONVERSATION_KEY.fullmatch(web_conversation_key):
            web_conversation_key = canonical_filed_as
            needs_rewrite = True
        if str(raw.get("web_conversation_key") or "") != web_conversation_key:
            needs_rewrite = True
        binding = _read_binding(raw.get("binding"), canonical)
        if raw.get("binding") and not binding:
            needs_rewrite = True
        chats.append({
            "id": chat_id,
            "workspace_id": workspace_id,
            "pair": canonical,
            "name": str(raw.get("name") or "Chat")[:80],
            "project": str(raw.get("project") or "")[:120],
            # The transcript key is an ownership capability, not mutable
            # registry data. Older registries could point a new pair chat at
            # an agent-name transcript. That transcript had no pair identity
            # and could contain work from a completely different pair. Always
            # derive the key from the canonical pair and chat id so malformed,
            # stale, and schema-v2 registries repair themselves on read.
            "filed_as": canonical_filed_as,
            "filed_as_version": filed_as_version,
            # Provider-effect safety and remote-thread ownership are distinct
            # from the immutable local transcript capability. An explicit
            # Start again rotates only this key, so an old uncertain delivery
            # remains auditable without poisoning the new provider thread.
            "web_conversation_key": web_conversation_key,
            "binding": binding,
            "created_at": str(raw.get("created_at") or ""),
            "updated_at": str(raw.get("updated_at") or ""),
            "legacy_recovered": bool(raw.get("legacy_recovered")),
            "legacy_source": str(raw.get("legacy_source") or "")[:160],
            "archived_at": str(raw.get("archived_at") or "")[:40],
            "web_legacy_candidate": (
                raw.get("web_legacy_candidate")
                if isinstance(raw.get("web_legacy_candidate"), bool) else None
            ),
        })
    active = value.get("active") if isinstance(value.get("active"), dict) else {}
    chosen_active = (
        value.get("chosen_active")
        if isinstance(value.get("chosen_active"), dict) else {}
    )
    known = value.get("known_pairs") if isinstance(value.get("known_pairs"), list) else []
    first_by_pair: set[str] = set()
    for one in chats:
        key = _pair_scope_key(str(one.get("workspace_id") or ""), one["pair"])
        inferred = (
            key not in first_by_pair
            and _may_adopt_legacy(str(one.get("workspace_id") or ""))
        )
        first_by_pair.add(key)
        if one["web_legacy_candidate"] is None:
            one["web_legacy_candidate"] = inferred
            needs_rewrite = True
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "revision": int(selected["revision"]),
        # The saved-board library is intentionally uncapped. A global slice
        # here let activity on later boards silently erase the registry index
        # for earlier boards. Capacity is enforced per workspace at creation;
        # every accepted workspace keeps all of its indexed chats.
        "chats": chats,
        "active": {str(key)[:400]: str(item) for key, item in active.items()},
        "chosen_active": {
            str(key)[:400]: str(item) for key, item in chosen_active.items()
        },
        "known_pairs": [str(one)[:400] for one in known if isinstance(one, str)],
        "_needs_rewrite": needs_rewrite or bool(recovered_from),
        "_recovered_from": recovered_from,
    }


def _atomic_text(config: LoadedConfig, where: Path, text: str) -> None:
    root = config.project_root.resolve()
    try:
        relative = where.relative_to(root)
    except ValueError as exc:
        raise swarm_lab.SwarmError(
            "A saved-chat artifact escaped the open project. Nothing was written."
        ) from exc
    where = _artifact_path(config, relative)
    where.parent.mkdir(parents=True, exist_ok=True)
    beside = _artifact_path(
        config,
        relative.with_name(
            f".{where.name}.{os.getpid()}-{threading.get_ident()}-{uuid.uuid4().hex}.part"
        ),
    )
    try:
        with beside.open("x", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(beside, where)
    finally:
        beside.unlink(missing_ok=True)


def _write(config: LoadedConfig, value: dict[str, Any]) -> None:
    where = _where(config)
    backup = _backup_where(config)
    where.parent.mkdir(parents=True, exist_ok=True)
    persisted = {
        key: held for key, held in value.items() if not str(key).startswith("_")
    }
    current_candidate = _registry_candidate(where)
    backup_candidate = _registry_candidate(backup)
    revisions = [
        int(one["revision"]) for one in (current_candidate, backup_candidate)
        if one is not None
    ]
    held_revision = value.get("revision", 0)
    if isinstance(held_revision, int) and not isinstance(held_revision, bool):
        revisions.append(max(0, held_revision))
    persisted["schema_version"] = REGISTRY_SCHEMA_VERSION
    persisted["revision"] = max(revisions, default=0) + 1
    persisted["integrity_version"] = REGISTRY_INTEGRITY_VERSION
    persisted.pop("integrity_sha256", None)
    persisted["integrity_sha256"] = _registry_integrity(persisted)
    written = json.dumps(persisted, indent=2) + "\n"

    # The registry is the only index that gives opaque transcript files their
    # names and owners. Keep the last known-good complete copy, and keep an
    # append-only snapshot before every structural chat change. A corrupt or
    # accidentally shortened registry must never turn existing transcripts
    # into invisible files on the next write.
    current = current_candidate["value"] if current_candidate is not None else None
    if current is not None:
        old_chats = json.dumps(current.get("chats", []), sort_keys=True)
        new_chats = json.dumps(persisted.get("chats", []), sort_keys=True)
        if old_chats != new_chats:
            history = _history_where(config)
            history.mkdir(parents=True, exist_ok=True)
            snapshot = _history_snapshot_where(
                config, f"_board-conversations-{time.time_ns()}.json",
            )
            _atomic_text(config, snapshot, json.dumps(current, indent=2) + "\n")

    _atomic_text(config, where, written)
    _atomic_text(config, backup, written)
    value["revision"] = int(persisted["revision"])


def _agents(board: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(one.get("id")): one for one in board.get("agents", [])
        if isinstance(one, dict) and one.get("id")
    }


def _pair(one: str, other: str = "") -> list[str]:
    return sorted([one] if not other or other == one else [one, other])


def _pair_key(pair: list[str]) -> str:
    return "|".join(pair)


def _route_binding(config: LoadedConfig, member: dict[str, Any]) -> dict[str, Any]:
    route = str(member.get("who") or "")
    _kind, context = chat_lab._route_failure_context(  # noqa: SLF001 - shared contract
        config, route
    )
    return {
        "route": route, **context,
        "effective_dispatch_strength": "verified",
    }


def _web_route_binding_for_contract(
    config: LoadedConfig, route: str, transport_contract: str,
) -> dict[str, Any]:
    """Recompute one historical web binding from current non-secret config.

    The v1 relay used the same route/profile material as v2 and differed only
    in its transport contract revision. Keeping the recomputation here makes
    the compatibility bridge independent from the live v2 context helper: a
    later v3 bump cannot accidentally make v1 records eligible again.
    """

    named = str(route or "").strip()
    if not named.startswith("web:"):
        return {}
    routes = config.get("providers", {}) or {}
    profile = routes.get(named) if isinstance(routes, dict) else None
    if not isinstance(profile, dict):
        profile = {}
    safe_profile = CredentialRedactor(config).value(profile)
    effective = effective_dispatch_fingerprint(
        WEB_EFFECTIVE_DISPATCH_CONTRACT,
        {
            "route": named,
            "profile": safe_profile,
            "transport_contract": transport_contract,
        },
    )
    canonical = json.dumps(
        {
            "route": named,
            "kind": "web-chat",
            "profile": profile,
            "transport_contract": transport_contract,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=lambda value: f"<{type(value).__name__}>",
    ).encode("utf-8")
    return {
        "route": named,
        "failure_context_version": chat_lab.FAILURE_CONTEXT_VERSION,
        "route_fingerprint_sha256": hashlib.sha256(canonical).hexdigest(),
        "transport_contract": transport_contract,
        **effective,
        "effective_dispatch_strength": "verified",
    }


def _project_path_fingerprint(path: str) -> str:
    held = str(path or "")
    canonical = (
        os.path.normcase(str(Path(held).resolve(strict=False))) if held else ""
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _filesystem_project_identity(path: str) -> dict[str, Any]:
    """Describe one existing directory without creating state inside it.

    A canonical path alone survives replacement of the directory at that path.
    The local filesystem's device/file identity distinguishes that replacement.
    Persist only a versioned digest, not platform-specific raw identifiers.
    """

    held = str(path or "")
    canonical = (
        os.path.normcase(str(Path(held).resolve(strict=False))) if held else ""
    )
    path_fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if not canonical:
        return {
            "path_fingerprint_sha256": path_fingerprint,
            "identity_strength": "unavailable",
            "directory_identity_version": PROJECT_DIRECTORY_IDENTITY_VERSION,
            "directory_identity_sha256": "",
        }
    try:
        found = Path(canonical).stat()
    except OSError:
        found = None
    file_id = int(getattr(found, "st_ino", 0) or 0) if found is not None else 0
    if found is None or not stat.S_ISDIR(found.st_mode) or file_id <= 0:
        return {
            "path_fingerprint_sha256": path_fingerprint,
            "identity_strength": "unavailable",
            "directory_identity_version": PROJECT_DIRECTORY_IDENTITY_VERSION,
            "directory_identity_sha256": "",
        }
    payload = {
        "directory_identity_version": PROJECT_DIRECTORY_IDENTITY_VERSION,
        "path_fingerprint_sha256": path_fingerprint,
        "device": str(int(found.st_dev)),
        "file_id": str(file_id),
    }
    digest = hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "path_fingerprint_sha256": path_fingerprint,
        "identity_strength": "filesystem",
        "directory_identity_version": PROJECT_DIRECTORY_IDENTITY_VERSION,
        "directory_identity_sha256": digest,
    }


def _project_binding(
    project: dict[str, Any] | None, project_id: str, *, legacy_path_only: bool = False,
) -> dict[str, Any]:
    if project is None or not project_id:
        return {
            "id": "",
            "path_fingerprint_sha256": "",
            "identity_strength": "none",
            "directory_identity_version": PROJECT_DIRECTORY_IDENTITY_VERSION,
            "directory_identity_sha256": "",
        }
    path = str(project.get("path") or "")
    if legacy_path_only:
        return {
            "id": project_id,
            "path_fingerprint_sha256": _project_path_fingerprint(path),
            "identity_strength": "legacy-path-only",
            "directory_identity_version": 0,
            "directory_identity_sha256": "",
        }
    return {"id": project_id, **_filesystem_project_identity(path)}


def _binding_for(
    config: LoadedConfig, board: dict[str, Any], pair: list[str], project_id: str,
    *, legacy_path_only: bool = False,
) -> dict[str, Any]:
    agents = _agents(board)
    routes = {
        member_id: _route_binding(config, agents.get(member_id) or {})
        for member_id in pair
    }
    if legacy_path_only:
        for route_binding in routes.values():
            for key in (
                "effective_dispatch_version",
                "effective_dispatch_fingerprint_sha256",
                "effective_dispatch_contract",
            ):
                route_binding.pop(key, None)
            route_binding["effective_dispatch_strength"] = "legacy-unverified"
    project = next((
        one for one in board.get("projects", [])
        if isinstance(one, dict) and str(one.get("id") or "") == project_id
    ), None)
    return {
        "binding_schema_version": (
            1 if legacy_path_only else CHAT_BINDING_SCHEMA_VERSION
        ),
        "agent_routes": routes,
        "project": _project_binding(
            project, project_id if project else "",
            legacy_path_only=legacy_path_only,
        ),
    }


def _read_binding(value: Any, pair: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    schema_version = value.get("binding_schema_version")
    if (
        isinstance(schema_version, bool) or not isinstance(schema_version, int)
        or schema_version not in {
            CHAT_BINDING_SCHEMA_VERSION, *MIGRATABLE_CHAT_BINDING_SCHEMAS,
        }
    ):
        return {}
    raw_routes = value.get("agent_routes")
    if not isinstance(raw_routes, dict) or set(raw_routes) != set(pair):
        return {}
    routes: dict[str, dict[str, Any]] = {}
    for member_id in pair:
        raw = raw_routes.get(member_id)
        if not isinstance(raw, dict):
            return {}
        fingerprint = str(raw.get("route_fingerprint_sha256") or "").lower()
        contract = str(raw.get("transport_contract") or "")[:160]
        context_version = raw.get("failure_context_version")
        if (
            not _SHA256.fullmatch(fingerprint) or not contract
            or not isinstance(context_version, int)
        ):
            return {}
        route_binding = {
            "route": str(raw.get("route") or "")[:64],
            "failure_context_version": context_version,
            "route_fingerprint_sha256": fingerprint,
            "transport_contract": contract,
        }
        if schema_version in LEGACY_EFFECTIVE_DISPATCH_SCHEMAS:
            route_binding["effective_dispatch_strength"] = "legacy-unverified"
        else:
            effective_version = raw.get("effective_dispatch_version")
            effective_fingerprint = str(
                raw.get("effective_dispatch_fingerprint_sha256") or ""
            ).lower()
            effective_contract = str(
                raw.get("effective_dispatch_contract") or ""
            )[:160]
            if (
                isinstance(effective_version, bool)
                or not isinstance(effective_version, int)
                or effective_version < 1
                or not _SHA256.fullmatch(effective_fingerprint)
                or not effective_contract
            ):
                return {}
            route_binding.update({
                "effective_dispatch_version": effective_version,
                "effective_dispatch_fingerprint_sha256": effective_fingerprint,
                "effective_dispatch_contract": effective_contract,
                "effective_dispatch_strength": "verified",
            })
        routes[member_id] = route_binding
    raw_project = value.get("project")
    if not isinstance(raw_project, dict):
        return {}
    project_id = str(raw_project.get("id") or "")[:120]
    path_fingerprint = str(
        raw_project.get("path_fingerprint_sha256") or ""
    ).lower()
    if project_id and not _SHA256.fullmatch(path_fingerprint):
        return {}
    if not project_id:
        path_fingerprint = ""
    if schema_version in LEGACY_PATH_ONLY_CHAT_BINDING_SCHEMAS:
        strength = "legacy-path-only" if project_id else "none"
        identity_version = 0 if project_id else PROJECT_DIRECTORY_IDENTITY_VERSION
        identity_fingerprint = ""
    else:
        strength = str(raw_project.get("identity_strength") or "")
        identity_version = raw_project.get("directory_identity_version")
        identity_fingerprint = str(
            raw_project.get("directory_identity_sha256") or ""
        ).lower()
        if (
            isinstance(identity_version, bool)
            or identity_version != PROJECT_DIRECTORY_IDENTITY_VERSION
            or strength not in {"none", "filesystem", "unavailable"}
        ):
            return {}
        if not project_id:
            if strength != "none" or identity_fingerprint:
                return {}
        elif strength == "filesystem":
            if not _SHA256.fullmatch(identity_fingerprint):
                return {}
        elif strength != "unavailable" or identity_fingerprint:
            return {}
    return {
        "binding_schema_version": schema_version,
        "agent_routes": routes,
        "project": {
            "id": project_id,
            "path_fingerprint_sha256": path_fingerprint,
            "identity_strength": strength,
            "directory_identity_version": identity_version,
            "directory_identity_sha256": identity_fingerprint,
        },
    }


def _adopt_legacy_registry(
    config: LoadedConfig, registry: dict[str, Any], board: dict[str, Any],
) -> bool:
    """Bind pre-v6 chats once to the exact legacy live board.

    A named snapshot/import must never opportunistically claim unscoped chats.
    Board-save fencing calls this before a workspace switch, so the real legacy
    live board still gets its history even if the first post-upgrade action is
    opening another saved board.
    """

    workspace_id = _board_workspace_id(board)
    if not _may_adopt_legacy(workspace_id):
        return False
    agents = _agents(board)
    changed = False
    adopted_ids: set[str] = set()
    for conversation in registry.get("chats", []):
        if conversation.get("workspace_id"):
            continue
        pair = conversation.get("pair", [])
        if not isinstance(pair, list) or any(one not in agents for one in pair):
            continue
        conversation["workspace_id"] = workspace_id
        conversation["filed_as_version"] = 1
        conversation["binding"] = _binding_for(
            config, board, pair, str(conversation.get("project") or ""),
            legacy_path_only=True,
        )
        adopted_ids.add(str(conversation.get("id") or ""))
        changed = True

    for agent_id in agents:
        scoped = _scoped_key(workspace_id, agent_id)
        for field in ("active", "chosen_active"):
            old = str(registry[field].get(agent_id) or "")
            if old in adopted_ids and not registry[field].get(scoped):
                registry[field][scoped] = old
                changed = True
            if agent_id in registry[field]:
                registry[field].pop(agent_id, None)
                changed = True
    for old in list(registry.get("known_pairs", [])):
        pair = str(old).split("|")
        if len(pair) not in (1, 2) or any(one not in agents for one in pair):
            continue
        scoped = _pair_scope_key(workspace_id, sorted(pair))
        if scoped not in registry["known_pairs"]:
            registry["known_pairs"].append(scoped)
        registry["known_pairs"].remove(old)
        changed = True
    return changed


def _binding_problem(
    config: LoadedConfig, board: dict[str, Any], raw: dict[str, Any],
    agents: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    binding = raw.get("binding")
    if not binding:
        return {
            "code": "binding_unknown",
            "message": (
                "This older chat has no verified provider binding. Nexus kept its "
                "transcript and will not guess where to send it. Start a fresh chat "
                "with the current setup."
            ),
            "action": "start_fresh",
            "action_label": "Start fresh with current setup",
        }
    changed_routes: list[dict[str, str]] = []
    for member_id in raw["pair"]:
        member = agents.get(member_id) or {}
        current = _route_binding(config, member)
        held = binding["agent_routes"].get(member_id) or {}
        common_fields = (
            "route", "failure_context_version", "route_fingerprint_sha256",
            "transport_contract",
        )
        base_changed = any(
            held.get(key) != current.get(key) for key in common_fields
        )
        effective_changed = (
            held.get("effective_dispatch_strength") == "verified"
            and any(
                held.get(key) != current.get(key) for key in (
                    "effective_dispatch_version",
                    "effective_dispatch_fingerprint_sha256",
                    "effective_dispatch_contract",
                )
            )
        )
        if base_changed or effective_changed:
            kind = (
                "route_changed" if held.get("route") != current.get("route")
                else "route_settings_changed" if base_changed
                else "effective_dispatch_changed"
            )
            changed_routes.append({
                "agent_id": member_id,
                "agent_name": str(member.get("name") or member_id),
                "before_route": str(held.get("route") or ""),
                "current_route": str(current.get("route") or ""),
                "kind": kind,
            })
    if changed_routes:
        names = ", ".join(one["agent_name"] for one in changed_routes)
        route_renamed = any(one["kind"] == "route_changed" for one in changed_routes)
        dispatch_changed = any(
            one["kind"] == "effective_dispatch_changed" for one in changed_routes
        )
        detail = (
            "assistant route changed" if route_renamed
            else "effective provider executable or dispatch contract changed"
            if dispatch_changed else "connection settings changed"
        )
        owner = f"{names}'s" if len(changed_routes) == 1 else f"the setup for {names}"
        return {
            "code": "agent_binding_changed",
            "changed_agents": changed_routes,
            "message": (
                f"This chat is paused because {owner} {detail}. Nexus kept its "
                "transcript and will not send that history to a different provider "
                "setup. Start a fresh chat with the current setup."
            ),
            "action": "start_fresh",
            "action_label": "Start fresh with current setup",
        }

    held_project = binding.get("project") or {}
    project_id = str(raw.get("project") or "")
    project = next((
        one for one in board.get("projects", [])
        if isinstance(one, dict) and str(one.get("id") or "") == project_id
    ), None)
    current_path = _project_binding(
        project, project_id if project else "", legacy_path_only=True,
    )
    held_path = {
        "id": str(held_project.get("id") or ""),
        "path_fingerprint_sha256": str(
            held_project.get("path_fingerprint_sha256") or ""
        ),
    }
    current_path_fields = {
        "id": current_path["id"],
        "path_fingerprint_sha256": current_path["path_fingerprint_sha256"],
    }
    held_strength = str(held_project.get("identity_strength") or "")
    project_change_reason = ""
    current_strength = str(current_path.get("identity_strength") or "")
    if held_path != current_path_fields:
        project_change_reason = "project_path_changed"
    elif held_strength not in {"legacy-path-only", "none"}:
        current_project = _project_binding(project, project_id if project else "")
        current_strength = str(current_project.get("identity_strength") or "")
        if held_project != current_project:
            project_change_reason = "directory_identity_changed"
    if project_change_reason:
        detail = (
            "its selected project path now refers to a different local folder object"
            if project_change_reason == "directory_identity_changed"
            else "its selected project folder or project identity changed"
        )
        return {
            "code": "project_binding_changed",
            "project_id": project_id,
            "reason": project_change_reason,
            "binding_strength": held_strength,
            "current_binding_strength": current_strength,
            "message": (
                f"This chat is paused because {detail}. Nexus kept the transcript "
                "and will not apply its "
                "history to a different folder. Start a fresh chat with the current setup."
            ),
            "action": "start_fresh",
            "action_label": "Start fresh with current setup",
        }
    if project_id and project_id not in {
        str(one.get("id") or "") for one in _shared_projects(board, raw["pair"])
    }:
        return {
            "code": "project_access_changed",
            "project_id": project_id,
            "message": (
                "This chat is paused because its agents no longer share the selected "
                "project. Nexus kept the transcript. Reconnect the same project or "
                "start a fresh chat with the current setup."
            ),
            "action": "start_fresh",
            "action_label": "Start fresh with current setup",
        }
    return None


def fence_for_board_change(
    config: LoadedConfig, before: dict[str, Any], after: dict[str, Any]
) -> int:
    """Fence saved chats when any authority-bearing board binding changes."""

    def authority(board: dict[str, Any]) -> str:
        agents = sorted(
            (
                str(one.get("id") or ""), str(one.get("name") or ""),
                str(one.get("who") or ""), str(one.get("filed_as") or ""),
            )
            for one in board.get("agents", []) if isinstance(one, dict)
        )
        projects = sorted(
            (str(one.get("id") or ""), str(one.get("path") or ""))
            for one in board.get("projects", []) if isinstance(one, dict)
        )
        payload = {
            "workspace_id": _board_workspace_id(board),
            "agents": agents,
            "projects": projects,
            "works_on": sorted(
                (str(one.get("agent") or ""), str(one.get("project") or ""))
                for one in board.get("works_on", []) if isinstance(one, dict)
            ),
            "talks_to": sorted(
                tuple(sorted((str(one.get("one") or ""), str(one.get("other") or ""))))
                for one in board.get("talks_to", []) if isinstance(one, dict)
            ),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    changed_authority = authority(before) != authority(after)
    from .collaboration_ledger import fence_ledger

    with _registry_transaction(config):
        registry = _read(config)
        changed_registry = bool(registry.pop("_needs_rewrite", False))
        if _adopt_legacy_registry(config, registry, before):
            changed_registry = True
        if not changed_authority:
            if changed_registry:
                _write(config, registry)
            return 0
        before_workspace = _board_workspace_id(before)
        routes = {
            str(one.get("id") or ""): str(one.get("who") or "")
            for one in after.get("agents", []) if isinstance(one, dict)
        }
        old_routes = {
            str(one.get("id") or ""): str(one.get("who") or "")
            for one in before.get("agents", []) if isinstance(one, dict)
        }
        fenced = 0
        for conversation in registry.get("chats", []):
            if not isinstance(conversation, dict):
                continue
            if conversation.get("workspace_id") != before_workspace:
                continue
            pair = conversation.get("pair", [])
            first = str(pair[0]) if isinstance(pair, list) and pair else ""
            old_route = old_routes.get(first, "")
            new_route = routes.get(first, old_route)
            fence_ledger(
                config, old_route,
                str(conversation.get("filed_as") or ""),
            )
            if new_route != old_route:
                fence_ledger(
                    config, new_route, str(conversation.get("filed_as") or "")
                )
            fenced += 1
        if changed_registry:
            _write(config, registry)
        return fenced


def _connected_pairs(board: dict[str, Any], agent_id: str) -> list[list[str]]:
    agents = _agents(board)
    if agent_id not in agents:
        raise swarm_lab.SwarmError("That agent is not on the board any more. Refresh the board.")
    pairs = []
    for other in agents:
        if other != agent_id and swarm_lab.may_they_talk(board, agent_id, other):
            pairs.append(_pair(agent_id, other))
    # A single-agent chat remains usable on a board with no green line. As soon
    # as a peer is connected, the real pair workspaces replace this fallback.
    return pairs or [[agent_id]]


def _shared_projects(board: dict[str, Any], pair: list[str]) -> list[dict[str, Any]]:
    assigned = {
        agent: {
            str(line.get("project")) for line in board.get("works_on", [])
            if isinstance(line, dict) and str(line.get("agent")) == agent
        }
        for agent in pair
    }
    shared = set.intersection(*(assigned[agent] for agent in pair)) if pair else set()
    return [
        one for one in board.get("projects", [])
        if isinstance(one, dict) and str(one.get("id")) in shared
    ]


def _same_saved_route(
    held: dict[str, Any], current: dict[str, Any],
) -> bool:
    if any(
        held.get(key) != current.get(key) for key in (
            "route", "failure_context_version", "route_fingerprint_sha256",
            "transport_contract",
        )
    ):
        return False
    strength = str(held.get("effective_dispatch_strength") or "")
    if strength == "legacy-unverified":
        return True
    return strength == "verified" and all(
        held.get(key) == current.get(key) for key in (
            "effective_dispatch_version",
            "effective_dispatch_fingerprint_sha256",
            "effective_dispatch_contract",
        )
    )


def _same_saved_project_for_binding_upgrade(
    board: dict[str, Any], raw: dict[str, Any],
) -> bool:
    """Refuse a transport migration that would also rebind project authority."""

    binding = raw.get("binding") or {}
    held = binding.get("project") or {}
    project_id = str(raw.get("project") or "")
    shared = {
        str(one.get("id") or "") for one in _shared_projects(board, raw["pair"])
    }
    if project_id and project_id not in shared:
        return False
    project = next((
        one for one in board.get("projects", [])
        if isinstance(one, dict) and str(one.get("id") or "") == project_id
    ), None)
    current_path = _project_binding(
        project, project_id if project else "", legacy_path_only=True,
    )
    if any(
        str(held.get(key) or "") != str(current_path.get(key) or "")
        for key in ("id", "path_fingerprint_sha256")
    ):
        return False
    strength = str(held.get("identity_strength") or "")
    if strength == "legacy-path-only":
        return bool(project_id)
    if strength == "none":
        return not project_id
    return held == _project_binding(project, project_id if project else "")


def _upgrade_exact_legacy_web_binding(
    config: LoadedConfig, board: dict[str, Any], raw: dict[str, Any],
    agents: dict[str, dict[str, Any]],
) -> bool:
    """Upgrade only the proven relay-v1 route fields of one saved binding.

    Binding schemas 1 and 2 predate a persisted effective-dispatch digest. For
    web routes, their exact route digest still binds the complete input to that
    second digest (route, profile, and transport contract), so it is sufficient
    evidence for this one bounded bridge. Other providers remain legacy and
    unverified; the migration never blesses a changed executable for them.
    """

    binding = raw.get("binding") or {}
    held_routes = binding.get("agent_routes") or {}
    schema_version = binding.get("binding_schema_version")
    if schema_version not in {
        CHAT_BINDING_SCHEMA_VERSION, *MIGRATABLE_CHAT_BINDING_SCHEMAS,
    }:
        return False
    if not any(
        isinstance(one, dict)
        and one.get("transport_contract") == LEGACY_WEB_RELAY_CONTRACT
        for one in held_routes.values()
    ):
        return False
    current_routes = {
        member_id: _route_binding(config, agents.get(member_id) or {})
        for member_id in raw.get("pair", [])
    }
    upgrades: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for member_id in raw.get("pair", []):
        held = held_routes.get(member_id) or {}
        current = current_routes.get(member_id) or {}
        if _same_saved_route(held, current):
            continue
        route = str(current.get("route") or "")
        expected_legacy = _web_route_binding_for_contract(
            config, route, LEGACY_WEB_RELAY_CONTRACT,
        )
        expected_current = _web_route_binding_for_contract(
            config, route, CURRENT_WEB_RELAY_CONTRACT,
        )
        if (
            not expected_legacy
            or current.get("transport_contract") != CURRENT_WEB_RELAY_CONTRACT
            or not _same_saved_route(current, expected_current)
            or any(
                held.get(key) != expected_legacy.get(key) for key in (
                    "route", "failure_context_version",
                    "route_fingerprint_sha256", "transport_contract",
                )
            )
        ):
            return False
        strength = str(held.get("effective_dispatch_strength") or "")
        if strength == "verified":
            if any(
                held.get(key) != expected_legacy.get(key) for key in (
                    "effective_dispatch_version",
                    "effective_dispatch_fingerprint_sha256",
                    "effective_dispatch_contract",
                )
            ):
                return False
        elif (
            strength != "legacy-unverified"
            or schema_version not in LEGACY_EFFECTIVE_DISPATCH_SCHEMAS
        ):
            return False
        upgrades.append((held, current))
    if not upgrades or not _same_saved_project_for_binding_upgrade(board, raw):
        return False
    for held, current in upgrades:
        for key in (
            "route", "failure_context_version", "route_fingerprint_sha256",
            "transport_contract",
        ):
            held[key] = current[key]
        if held.get("effective_dispatch_strength") == "verified":
            for key in (
                "effective_dispatch_version",
                "effective_dispatch_fingerprint_sha256",
                "effective_dispatch_contract",
            ):
                held[key] = current[key]
    return True


def _upgrade_exact_legacy_web_bindings(
    config: LoadedConfig, registry: dict[str, Any], board: dict[str, Any],
) -> bool:
    """Upgrade eligible chats while their whole-registry transaction is held."""

    workspace_id = _board_workspace_id(board)
    agents = _agents(board)
    changed = False
    for raw in registry.get("chats", []):
        if (
            raw.get("workspace_id") == workspace_id
            and all(member_id in agents for member_id in raw.get("pair", []))
            and _upgrade_exact_legacy_web_binding(
                config, board, raw, agents,
            )
        ):
            changed = True
    return changed


def _project_work_authority(project: dict[str, Any]) -> dict[str, Any]:
    """Read execution authority for the exact board-project target."""
    path = Path(str(project.get("path") or ""))
    if not bool(project.get("is_there", path.is_dir())):
        return {
            "can_run": False, "reason": "The selected project folder is not available.",
            "reason_code": "missing", "repairable": False, "fingerprint": "",
        }
    try:
        return pipeline_runs.inspect_project_authority(path)
    except (HarnessError, OSError) as exc:
        return {
            "can_run": False, "reason": str(exc),
            "reason_code": "unsafe_or_malformed", "repairable": False,
            "fingerprint": "",
        }


def _filed_as(pair: list[str], chat_id: str, workspace_id: str = "") -> str:
    # Version-one registries intentionally omit workspace_id so their exact
    # transcript filename survives migration. Every new chat binds the opaque
    # board scope into the capability and cannot collide with another board
    # that happens to reuse agent ids.
    identity = "|".join(pair) + "|" + chat_id
    if workspace_id:
        identity = workspace_id + "|" + identity
    marked = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"pair-chat-{marked}"


def _turn_ids(value: str) -> set[str]:
    return {
        one.strip() for one in str(value or "").split(",") if one.strip()
    }


def _owned_blocks(turns: list[chat_lab.Said], pair: list[str]) -> list[chat_lab.Said]:
    """Return only complete legacy blocks that prove this exact pair owns them."""

    target = set(pair)
    recovered: list[chat_lab.Said] = []
    block: list[chat_lab.Said] = []

    def keep_if_owned() -> None:
        if not block or block[0].who != "you":
            return
        # The user turn written by pair-aware builds names every intended
        # recipient. This is the ownership boundary. Speaker/recipient IDs on
        # all remaining turns must stay inside it as a second fail-closed check.
        prompt_recipients = _turn_ids(block[0].recipient_id)
        mentioned: set[str] = set()
        for turn in block:
            mentioned.update(_turn_ids(turn.speaker_id))
            mentioned.update(_turn_ids(turn.recipient_id))
        if prompt_recipients == target and mentioned and mentioned <= target:
            recovered.extend(block)

    for turn in turns:
        if turn.who == "you":
            keep_if_owned()
            block = [turn]
        elif block:
            block.append(turn)
    keep_if_owned()
    return recovered


def _recover_legacy_history(
    config: LoadedConfig, raw: dict[str, Any], agents: dict[str, dict[str, Any]]
) -> int:
    recovered: list[chat_lab.Said] = []
    for member_id in raw["pair"]:
        member = agents.get(member_id) or {}
        legacy = str(member.get("filed_as") or "") or swarm_lab.filed_as(
            str(member.get("name") or "")
        )
        if not legacy:
            continue
        turns = chat_lab.read_it(
            config, str(member.get("who") or ""), legacy
        )
        recovered.extend(_owned_blocks(turns, raw["pair"]))
    lead = agents.get(raw["pair"][0]) or {}
    return chat_lab.merge_transcript(
        config, str(lead.get("who") or ""), raw["filed_as"], recovered
    )


def _new_chat(
    config: LoadedConfig, registry: dict[str, Any], board: dict[str, Any],
    pair: list[str],
) -> dict[str, Any]:
    workspace_id = _board_workspace_id(board)
    same = [
        one for one in registry["chats"]
        if one["pair"] == pair and one.get("workspace_id") == workspace_id
    ]
    in_workspace = [
        one for one in registry["chats"]
        if one.get("workspace_id") == workspace_id
    ]
    if len(same) >= MOST_PER_PAIR:
        raise swarm_lab.SwarmError(
            "This pair already has the maximum number of saved chats."
        )
    if len(in_workspace) >= MOST_CHATS:
        raise swarm_lab.SwarmError(
            "This saved board already has the maximum number of chats. "
            "Other saved boards and their chats are unaffected."
        )
    chat_id = f"chat-{uuid.uuid4().hex[:16]}"
    projects = _shared_projects(board, pair)
    now = _now()
    filed_as = _filed_as(pair, chat_id, workspace_id)
    made = {
        "id": chat_id,
        "workspace_id": workspace_id,
        "pair": pair,
        "name": f"Chat {len(same) + 1}",
        "project": str(projects[0].get("id")) if projects else "",
        "filed_as": filed_as,
        "filed_as_version": 2,
        "web_conversation_key": filed_as,
        "binding": _binding_for(
            config, board, pair, str(projects[0].get("id")) if projects else ""
        ),
        "created_at": now,
        "updated_at": now,
        # Legacy history belongs to Chat 1 only. A second chat is a deliberate
        # fresh workspace and must not repeat recovered history.
        "legacy_recovered": bool(same),
        "legacy_source": "",
        "archived_at": "",
        # One pre-isolation provider URL can be adopted by the first Nexus chat
        # for this pair. Every later chat must open a new remote thread.
        "web_legacy_candidate": (
            not bool(same) and _may_adopt_legacy(workspace_id)
        ),
    }
    registry["chats"].append(made)
    return made


def _legacy_names(member: dict[str, Any]) -> list[str]:
    """Stable pre-pair transcript names that can belong to this exact agent."""

    names: list[str] = []
    for candidate in (member.get("filed_as"), member.get("name")):
        value = " ".join(str(candidate or "").split())
        if value and value not in names:
            names.append(value)
    return names


def _copy_legacy_attachments(
    config: LoadedConfig, route: str, source: str, destination: str
) -> None:
    source_folder = chat_lab._attachment_folder(config, route, source)
    if not source_folder.is_dir() or source_folder.is_symlink():
        return
    destination_folder = chat_lab._attachment_folder(config, route, destination)
    destination_folder.mkdir(parents=True, exist_ok=True)
    for item in source_folder.iterdir():
        if not item.is_file() or item.is_symlink():
            continue
        target = destination_folder / item.name
        if not target.exists():
            shutil.copy2(item, target)


def _recover_direct_legacy_chats(
    config: LoadedConfig, registry: dict[str, Any], board: dict[str, Any],
    agent_id: str, agents: dict[str, dict[str, Any]],
) -> bool:
    """Index old agent-owned chats without pretending they belonged to a pair.

    Before pair chats existed, one stable board-agent name owned one transcript.
    Those files were deliberately left untouched by the pair migration, but no
    UI indexed them afterwards. Copy each one into a canonical single-agent
    conversation and retain the source as immutable recovery evidence.
    """

    member = agents.get(agent_id) or {}
    route = str(member.get("who") or "")
    workspace_id = _board_workspace_id(board)
    changed = False
    for legacy_name in _legacy_names(member):
        source = chat_lab.where_it_is_kept(config, route, legacy_name)
        if not source.is_file():
            continue
        source_key = source.name
        if any(
            one.get("legacy_source") == source_key and one.get("pair") == [agent_id]
            and one.get("workspace_id") == workspace_id
            for one in registry["chats"]
        ):
            continue
        turns = chat_lab.read_it(config, route, legacy_name)
        if not turns:
            continue
        made = _new_chat(config, registry, board, [agent_id])
        made["name"] = "Recovered older chat"
        made["project"] = ""
        made["binding"] = _binding_for(config, board, [agent_id], "")
        made["legacy_recovered"] = True
        made["legacy_source"] = source_key
        made["web_legacy_candidate"] = False
        made["created_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(source.stat().st_mtime)
        )
        made["updated_at"] = _now()
        chat_lab.merge_transcript(config, route, made["filed_as"], turns)
        _copy_legacy_attachments(config, route, legacy_name, made["filed_as"])
        key = _pair_scope_key(workspace_id, [agent_id])
        if key not in registry["known_pairs"]:
            registry["known_pairs"].append(key)
        changed = True
    return changed


def _present(
    config: LoadedConfig, board: dict[str, Any], agent_id: str,
    raw: dict[str, Any], agents: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    pair_agents = [agents[one] for one in raw["pair"] if one in agents]
    projects = _shared_projects(board, raw["pair"])
    project_ids = {str(one.get("id")) for one in projects}
    project_id = raw["project"] if raw["project"] in project_ids else ""
    presented_projects = [{
        "id": str(one.get("id") or ""),
        "name": str(one.get("name") or Path(str(one.get("path") or "")).name),
        "path": str(one.get("path") or ""),
        "is_there": bool(one.get("is_there", Path(str(one.get("path") or "")).is_dir())),
        "work_authority": _project_work_authority(one),
    } for one in projects]
    active_project = next((
        one for one in presented_projects if one["id"] == project_id
    ), None)
    current = agents[agent_id]
    binding_problem = _binding_problem(config, board, raw, agents)
    bound_route = str(
        ((raw.get("binding") or {}).get("agent_routes") or {})
        .get(agent_id, {}).get("route") or current.get("who") or ""
    )
    destination = {} if binding_problem else chat_lab.chat_destination(
        config, str(current.get("who") or ""), raw["filed_as"],
        conversation_key=str(
            raw.get("web_conversation_key") or raw["filed_as"]
        ),
        prefer_existing_conversation=bool(raw.get("web_legacy_candidate")),
    )
    collaboration_problem = None
    if not raw.get("archived_at"):
        from .collaboration_ledger import collaboration_problem as inspect_ledger

        collaboration_problem = inspect_ledger(
            config, bound_route, str(raw.get("filed_as") or ""),
        )
    return {
        **raw,
        "project": project_id,
        "pair_agents": [{
            "id": str(one.get("id") or ""),
            "name": str(one.get("name") or "An agent"),
            "who": str(one.get("who") or ""),
            "ready": bool(one.get("ready")),
        } for one in pair_agents],
        "projects": presented_projects,
        "work_authority": (
            dict(active_project["work_authority"]) if active_project else {
                "can_run": False,
                "reason": "Choose this chat's active project before starting file work.",
                "reason_code": "no_project", "repairable": False, "fingerprint": "",
            }
        ),
        "connected": len(raw["pair"]) == 1 or (
            len(raw["pair"]) == 2
            and swarm_lab.may_they_talk(board, raw["pair"][0], raw["pair"][1])
        ),
        "binding_problem": binding_problem,
        "collaboration_problem": collaboration_problem,
        # A legacy path-only binding remains usable for compatibility, but the
        # API says so explicitly instead of presenting it as filesystem-verified.
        "project_binding_strength": str(
            (((raw.get("binding") or {}).get("project") or {})
             .get("identity_strength") or "unknown")
        ),
        "effective_dispatch_strength": (
            "verified" if raw["pair"] and all(
                str((((raw.get("binding") or {}).get("agent_routes") or {})
                    .get(member_id, {}).get("effective_dispatch_strength") or ""))
                == "verified"
                for member_id in raw["pair"]
            ) else "legacy-unverified"
        ),
        "transcript_route": bound_route,
        "destination": destination,
    }


def _validated_conversation(
    config: LoadedConfig, registry: dict[str, Any], board: dict[str, Any],
    agent_id: str, chat_id: str, *, require_current_binding: bool = True,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Validate one active conversation against one registry snapshot."""

    if not _CHAT_ID.fullmatch(str(chat_id or "")):
        raise swarm_lab.SwarmError("Choose a saved chat first.")
    raw = next((one for one in registry["chats"] if one["id"] == chat_id), None)
    agents = _agents(board)
    if raw is None or agent_id not in raw["pair"]:
        raise swarm_lab.SwarmError("That chat does not belong to this agent pair.")
    if raw.get("archived_at"):
        raise swarm_lab.SwarmError("Restore this archived chat before using it.")
    if any(one not in agents for one in raw["pair"]):
        raise swarm_lab.SwarmError("One of this chat's agents is no longer on the board.")
    if len(raw["pair"]) == 2 and not swarm_lab.may_they_talk(
        board, raw["pair"][0], raw["pair"][1]
    ):
        raise swarm_lab.SwarmError("Reconnect these two agents before using this chat.")
    workspace_id = _board_workspace_id(board)
    if raw.get("workspace_id") != workspace_id:
        raise swarm_lab.SwarmError(
            "That chat belongs to a different saved board. Start a fresh chat on this board."
        )
    if require_current_binding:
        problem = _binding_problem(config, board, raw, agents)
        if problem:
            raise swarm_lab.SwarmError(str(problem["message"]))
    return raw, agents


def list_for_agent(
    config: LoadedConfig, board: dict[str, Any], agent_id: str
) -> dict[str, Any]:
    """List chats containing this agent, creating one initial chat per new pair."""

    with _registry_transaction(config):
        registry = _read(config)
        changed = bool(registry.pop("_needs_rewrite", False))
        recovered_from = str(registry.get("_recovered_from") or "")
        agents = _agents(board)
        workspace_id = _board_workspace_id(board)
        if _adopt_legacy_registry(config, registry, board):
            changed = True
        if _upgrade_exact_legacy_web_bindings(config, registry, board):
            changed = True
        pairs = _connected_pairs(board, agent_id)
        for pair in pairs:
            key = _pair_scope_key(workspace_id, pair)
            if key in registry["known_pairs"]:
                continue
            # Pair chats start in pair-owned storage. A pre-multi-chat file is
            # intentionally left intact as legacy history: it has no reliable
            # pair identity, so adopting it would relabel unknown speakers as
            # members of whichever pair happened to be listed first.
            made = _new_chat(config, registry, board, pair)
            registry["known_pairs"].append(key)
            changed = True

        if _may_adopt_legacy(workspace_id) and _recover_direct_legacy_chats(
            config, registry, board, agent_id, agents
        ):
            changed = True

        visible = [
            one for one in registry["chats"]
            if one.get("workspace_id") == workspace_id
            and agent_id in one["pair"]
            and all(member in agents for member in one["pair"])
        ]
        first_for_pair = {}
        for one in registry["chats"]:
            first_for_pair.setdefault(
                _pair_scope_key(str(one.get("workspace_id") or ""), one["pair"]),
                one["id"],
            )
        for one in visible:
            if one.get("legacy_recovered"):
                continue
            if _may_adopt_legacy(workspace_id) and first_for_pair.get(
                _pair_scope_key(workspace_id, one["pair"])
            ) == one["id"]:
                _recover_legacy_history(config, one, agents)
            one["legacy_recovered"] = True
            one["updated_at"] = _now()
            changed = True
        usable_ids = {one["id"] for one in visible if not one.get("archived_at")}
        selection_key = _scoped_key(workspace_id, agent_id)
        chosen = str(registry["chosen_active"].get(selection_key) or "")
        if chosen not in usable_ids:
            if chosen:
                registry["chosen_active"].pop(selection_key, None)
                changed = True
            chosen = ""

        active = str(registry["active"].get(selection_key) or "")
        if chosen:
            preferred = chosen
        else:
            # A board can connect one agent to several peers. Board order is
            # not a useful default: the first pair may have no folder in
            # common while a later pair is ready for project work. Until the
            # user explicitly picks a pair, prefer a conversation that has a
            # real shared project and therefore can do the work advertised by
            # the chat controls.
            preferred_chat = next((
                one for one in visible
                if not one.get("archived_at") and not one.get("legacy_source")
                and _shared_projects(board, one["pair"])
            ), next((
                one for one in visible if not one.get("archived_at")
            ), None))
            preferred = preferred_chat["id"] if preferred_chat else ""
        if active != preferred:
            active = preferred
            registry["active"][selection_key] = active
            changed = True
        if changed:
            _write(config, registry)
        return {
            "agent": agent_id,
            "workspace_id": workspace_id,
            "active": active,
            "registry_recovered_from": recovered_from,
            "chats": [_present(config, board, agent_id, one, agents) for one in visible],
        }


def resolve(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, chat_id: str,
    *, allow_binding_drift: bool = False,
) -> dict[str, Any]:
    """Resolve and validate one conversation for one of its pair members."""

    with _registry_transaction(config):
        registry = _read(config)
        changed = bool(registry.pop("_needs_rewrite", False))
        if _adopt_legacy_registry(config, registry, board):
            changed = True
        if _upgrade_exact_legacy_web_bindings(config, registry, board):
            changed = True
        if changed:
            _write(config, registry)
        raw, agents = _validated_conversation(
            config, registry, board, agent_id, chat_id,
            require_current_binding=not allow_binding_drift,
        )
        presented = _present(config, board, agent_id, raw, agents)
        presented["peer"] = next((one for one in raw["pair"] if one != agent_id), "")
        return presented


def create(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, peer_id: str,
    *, scope: str = "",
) -> dict[str, Any]:
    allowed = _connected_pairs(board, agent_id)
    requested_scope = str(scope or "").strip()
    if requested_scope not in ("", "single"):
        raise swarm_lab.SwarmError("Choose a supported saved-chat scope.")
    if requested_scope == "single":
        if peer_id:
            raise swarm_lab.SwarmError(
                "A single-agent chat cannot also name a peer."
            )
        pair = [agent_id]
    else:
        pair = _pair(agent_id, peer_id)
    # ``_connected_pairs`` intentionally returns the lone-agent fallback only
    # while an agent has no green lines. It also drives automatic Chat 1
    # creation, so adding singleton pairs there would silently create an extra
    # direct chat for every connected agent. A user-requested ``single`` scope
    # is the narrow, explicit exception: it preserves the exact one-agent
    # identity even when this agent also has connected pair workspaces.
    if pair not in allowed and not (
        requested_scope == "single" and pair == [agent_id]
    ):
        raise swarm_lab.SwarmError("These two agents need a green communication line first.")
    with _registry_transaction(config):
        registry = _read(config)
        _adopt_legacy_registry(config, registry, board)
        made = _new_chat(config, registry, board, pair)
        workspace_id = _board_workspace_id(board)
        key = _pair_scope_key(workspace_id, pair)
        if key not in registry["known_pairs"]:
            registry["known_pairs"].append(key)
        selection_key = _scoped_key(workspace_id, agent_id)
        registry["active"][selection_key] = made["id"]
        registry["chosen_active"][selection_key] = made["id"]
        _write(config, registry)
    return list_for_agent(config, board, agent_id)


def activate(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, chat_id: str
) -> dict[str, Any]:
    with _registry_transaction(config):
        registry = _read(config)
        _adopt_legacy_registry(config, registry, board)
        _validated_conversation(
            config, registry, board, agent_id, chat_id,
            require_current_binding=False,
        )
        selection_key = _scoped_key(_board_workspace_id(board), agent_id)
        registry["active"][selection_key] = chat_id
        registry["chosen_active"][selection_key] = chat_id
        _write(config, registry)
    return list_for_agent(config, board, agent_id)


def select_project(
    config: LoadedConfig, board: dict[str, Any], agent_id: str,
    chat_id: str, project_id: str,
) -> dict[str, Any]:
    with _registry_transaction(config):
        registry = _read(config)
        _adopt_legacy_registry(config, registry, board)
        _upgrade_exact_legacy_web_bindings(config, registry, board)
        raw, agents_by_id = _validated_conversation(
            config, registry, board, agent_id, chat_id,
            require_current_binding=False,
        )
        problem = _binding_problem(config, board, raw, agents_by_id)
        if problem:
            raise swarm_lab.SwarmError(str(problem["message"]))
        valid = {
            str(one.get("id")) for one in _shared_projects(board, raw["pair"])
        }
        if project_id and project_id not in valid:
            raise swarm_lab.SwarmError(
                "Legacy connected-agent project work requires both agents to share the selected project."
            )
        current_binding = _binding_for(
            config, board, raw["pair"], project_id,
        )
        if project_id and (
            current_binding["project"].get("identity_strength") != "filesystem"
        ):
            raise swarm_lab.SwarmError(
                "Nexus cannot verify a stable local identity for that project folder. "
                "Restore or choose the folder before granting this chat project authority."
            )
        if str(raw.get("project") or "") != project_id:
            from .collaboration_ledger import fence_ledger

            current = _agents(board).get(agent_id) or {}
            fence_ledger(
                config, str(current.get("who") or ""), str(raw.get("filed_as") or "")
            )
        raw["project"] = project_id
        # Choosing the current setup is an explicit rebind. It upgrades a
        # readable legacy path-only binding to the current strong contract;
        # ordinary listing/inspection never performs this upgrade.
        raw["binding"] = current_binding
        raw["updated_at"] = _now()
        selection_key = _scoped_key(_board_workspace_id(board), agent_id)
        registry["active"][selection_key] = chat_id
        registry["chosen_active"][selection_key] = chat_id
        _write(config, registry)
    return list_for_agent(config, board, agent_id)


def restart_provider_conversation(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, chat_id: str,
) -> dict[str, Any]:
    """Rotate one saved chat onto a fresh provider conversation identity.

    The local transcript key is intentionally stable. The remote identity is
    not: after an explicit Start again, retaining it would let a durable
    ``delivery_unknown`` fence for the discarded provider thread block the new
    conversation forever. A fresh persisted key gives Electron a new thread
    and leaves the old effect journal untouched for audit/reconciliation.
    """

    with _registry_transaction(config):
        registry = _read(config)
        _adopt_legacy_registry(config, registry, board)
        _upgrade_exact_legacy_web_bindings(config, registry, board)
        raw, agents = _validated_conversation(
            config, registry, board, agent_id, chat_id,
        )
        filed_as = str(raw.get("filed_as") or "")
        fresh = f"{filed_as}-restart-{uuid.uuid4().hex[:16]}"
        if not _WEB_CONVERSATION_KEY.fullmatch(fresh):
            raise swarm_lab.SwarmError(
                "Nexus could not create a safe fresh provider conversation identity."
            )
        raw["web_conversation_key"] = fresh
        raw["web_legacy_candidate"] = False
        raw["updated_at"] = _now()
        _write(config, registry)
        return _present(config, board, agent_id, raw, agents)


def delete(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, chat_id: str
) -> dict[str, Any]:
    """Archive one chat without removing any transcript or attachment."""

    with _registry_transaction(config):
        registry = _read(config)
        agents = _agents(board)
        raw = next((one for one in registry["chats"] if one["id"] == chat_id), None)
        if (
            raw is None or agent_id not in raw["pair"]
            or raw.get("workspace_id") != _board_workspace_id(board)
        ):
            raise swarm_lab.SwarmError("That chat does not belong to this agent pair.")
        if any(one not in agents for one in raw["pair"]):
            raise swarm_lab.SwarmError("One of this chat's agents is no longer on the board.")
        raw["archived_at"] = _now()
        raw["updated_at"] = raw["archived_at"]
        # Archiving is an authority revocation. Fence the exact transcript
        # before changing discoverability so late provider replies cannot
        # append to an archived conversation.
        from .collaboration_ledger import fence_ledger

        lead = agents.get(raw["pair"][0]) or {}
        fence_ledger(
            config, str(lead.get("who") or ""), str(raw.get("filed_as") or "")
        )
        for key, active in list(registry["active"].items()):
            if active == chat_id:
                registry["active"].pop(key, None)
        for key, chosen in list(registry["chosen_active"].items()):
            if chosen == chat_id:
                registry["chosen_active"].pop(key, None)
        _write(config, registry)
    return list_for_agent(config, board, agent_id)


def restore(
    config: LoadedConfig, board: dict[str, Any], agent_id: str, chat_id: str
) -> dict[str, Any]:
    """Restore one archived chat and make it active again."""

    if not _CHAT_ID.fullmatch(str(chat_id or "")):
        raise swarm_lab.SwarmError("Choose an archived chat first.")
    with _registry_transaction(config):
        registry = _read(config)
        agents = _agents(board)
        raw = next((one for one in registry["chats"] if one["id"] == chat_id), None)
        if (
            raw is None or agent_id not in raw["pair"]
            or raw.get("workspace_id") != _board_workspace_id(board)
        ):
            raise swarm_lab.SwarmError("That chat does not belong to this agent pair.")
        if any(one not in agents for one in raw["pair"]):
            raise swarm_lab.SwarmError("One of this chat's agents is no longer on the board.")
        raw["archived_at"] = ""
        raw["updated_at"] = _now()
        selection_key = _scoped_key(_board_workspace_id(board), agent_id)
        registry["active"][selection_key] = chat_id
        registry["chosen_active"][selection_key] = chat_id
        _write(config, registry)
    return list_for_agent(config, board, agent_id)
