"""Optional local embeddings, prepared off the agent-turn path.

Only generated index data is written. Markdown and the collaboration scheduler
are never mutated here. A cold, busy or unavailable embedder means lexical search.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import math
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

VERSION = 1
CHUNKING_VERSION = 1
MAX_ROWS = 2048
MAX_BATCH = 32
MAX_QUERIES = 128
_lock = threading.Lock()
_workers = threading.BoundedSemaphore(2)
_states: dict[tuple[str, str], dict[str, Any]] = {}
_selected: dict[str, tuple[str, str]] = {}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def settings(value: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(value.get('embedding_url', 'http://127.0.0.1:11434')).rstrip('/')
    parsed = urlsplit(endpoint)
    if parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', '::1', 'localhost'} or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
        raise ValueError('Memory embeddings require a local loopback HTTP endpoint.')
    model = str(value.get('embedding_model', '')).strip()
    if not model or len(model) > 160:
        raise ValueError('Choose a local embedding model before enabling hybrid memory.')
    return {'url': endpoint, 'model': model, 'version': VERSION, 'chunking': CHUNKING_VERSION}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def request_json(url: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    request = Request(url, data=json.dumps(data).encode() if data is not None else None,
                      headers={'Content-Type': 'application/json'})
    # Never forward private notes through an environment proxy or redirect.
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=2) as response:
        body = response.read(4 * 1024 * 1024 + 1)
    if len(body) > 4 * 1024 * 1024:
        raise ValueError('Embedding response too large')
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError('Invalid embedding response')
    return result


def vector(value: Any, dimensions: int | None = None) -> list[float]:
    if not isinstance(value, list) or not 1 <= len(value) <= 4096 or (dimensions is not None and len(value) != dimensions):
        raise ValueError('Incompatible embedding dimensions')
    if any(type(x) not in (float, int) or not math.isfinite(x) for x in value):
        raise ValueError('Invalid embedding values')
    norm = math.sqrt(sum(x * x for x in value))
    if not norm or not math.isfinite(norm):
        raise ValueError('Invalid embedding norm')
    return [x / norm for x in value]


def _schema(db: Any) -> None:
    db.executescript('''
        CREATE TABLE IF NOT EXISTS semantic_chunks(
            chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
            contract TEXT NOT NULL, source_hash TEXT NOT NULL, vector TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS semantic_queries(
            query_hash TEXT PRIMARY KEY, contract TEXT NOT NULL,
            vector TEXT NOT NULL, updated REAL NOT NULL);
    ''')


def _prepare(index: Any, config: dict[str, Any], query: str, state: dict[str, Any]) -> None:
    try:
        binding = index.vault_root / '.nexus-project-memory.json'
        binding_hash = digest(binding.read_text(encoding='utf-8')) if binding.exists() else ''
        tags = request_json(config['url'] + '/api/tags').get('models', [])
        model = config['model']
        installed = next((one for one in tags if one.get('name') in {model, model + ':latest'}), None)
        model_hash = str((installed or {}).get('digest', ''))
        if not re.fullmatch(r'[a-fA-F0-9]{64}', model_hash):
            raise ValueError('Local model identity unavailable')
        response = request_json(config['url'] + '/api/embed', {'model': model, 'input': query, 'truncate': False})
        query_vector = vector(response['embeddings'][0])
        contract = digest({**config, 'model_digest': model_hash, 'dimensions': len(query_vector), 'vault': str(index.vault_root), 'binding': binding_hash})
        # A changed installed model invalidates prior in-process readiness too.
        with _lock:
            if state.get('contract') != contract:
                state['contract'] = ''
        with closing(index._connect()) as db:
            _schema(db)
            with _lock:
                if _selected.get(str(index.database_path)) != state['key']:
                    return
            db.execute('DELETE FROM semantic_chunks WHERE contract != ?', (contract,))
            db.execute('DELETE FROM semantic_queries WHERE contract != ?', (contract,))
            rows = db.execute('SELECT id, body FROM chunks ORDER BY id LIMIT ?', (MAX_ROWS,)).fetchall()
            cached = {row['chunk_id']: row['source_hash'] for row in db.execute('SELECT chunk_id, source_hash FROM semantic_chunks WHERE contract = ?', (contract,))}
            db.commit()
        pending = [row for row in rows if cached.get(row['id']) != digest(row['body'])][:MAX_BATCH]
        if (digest(binding.read_text(encoding='utf-8')) if binding.exists() else '') != binding_hash:
            raise ValueError('Project binding changed before embedding')
        if pending:
            response = request_json(config['url'] + '/api/embed', {'model': model, 'input': [row['body'] for row in pending], 'truncate': False})
            vectors = response.get('embeddings', [])
            if len(vectors) != len(pending):
                raise ValueError('Incomplete embedding batch')
            vectors = [vector(item, len(query_vector)) for item in vectors]
        else:
            vectors = []
        final_models = request_json(config['url'] + '/api/tags').get('models', [])
        final_model = next((one for one in final_models if one.get('name') in {model, model + ':latest'}), None)
        if str((final_model or {}).get('digest', '')) != model_hash:
            raise ValueError('Model changed during embedding')
        # Network work happened without a SQLite transaction. Recheck the
        # exact source before committing: edits during embedding win.
        with closing(index._connect()) as db:
            _schema(db)
            with _lock:
                if _selected.get(str(index.database_path)) != state['key']:
                    return
            if (digest(binding.read_text(encoding='utf-8')) if binding.exists() else '') != binding_hash:
                raise ValueError('Project binding changed during embedding')
            for row, embedded in zip(pending, vectors):
                current = db.execute('SELECT body FROM chunks WHERE id = ?', (row['id'],)).fetchone()
                if current is not None and current['body'] == row['body']:
                    db.execute('INSERT OR REPLACE INTO semantic_chunks VALUES(?, ?, ?, ?)',
                               (row['id'], contract, digest(row['body']), json.dumps(embedded)))
            db.execute('INSERT OR REPLACE INTO semantic_queries VALUES(?, ?, ?, ?)',
                       (digest(query), contract, json.dumps(query_vector), time.time()))
            db.execute('DELETE FROM semantic_queries WHERE query_hash NOT IN (SELECT query_hash FROM semantic_queries ORDER BY updated DESC LIMIT ?)', (MAX_QUERIES,))
            db.commit()
        with _lock:
            state.update(contract=contract, reason='ready', dimensions=len(query_vector))
    except Exception:
        # Optional retrieval cannot fail or pause an agent turn. Do not persist
        # endpoint exceptions, which can contain private request text.
        with _lock:
            state.update(contract='', reason='unavailable')
    finally:
        with _lock:
            state['running'] = False
            state['next_check'] = time.monotonic() + (30 if state.get('reason') == 'unavailable' else 1)
        _workers.release()


def search(index: Any, config: dict[str, Any], query: str, limit: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    binding = index.vault_root / '.nexus-project-memory.json'
    binding_hash = digest(binding.read_text(encoding='utf-8')) if binding.exists() else ''
    key = (str(index.database_path), digest(config) if not binding_hash else digest([config, binding_hash]))
    with _lock:
        _selected[str(index.database_path)] = key
        state = _states.setdefault(key, {'key': key, 'running': False, 'contract': '', 'reason': 'warming', 'next_check': 0})
        # Bound idle state; active jobs retain their own state reference.
        if len(_states) > 32:
            for old in list(_states):
                if old != key and not _states[old]['running']:
                    del _states[old]
                    if _selected.get(old[0]) == old:
                        _selected.pop(old[0], None)
                    break
        if not state['running'] and time.monotonic() >= state['next_check'] and _workers.acquire(blocking=False):
            state['running'] = True
            worker = threading.Thread(target=_prepare, args=(index, config, query, state), daemon=True, name='nexus-memory-embedding')
            state['thread'] = worker
            worker.start()
        contract = state.get('contract', '')
        trace = {'state': state['reason'], 'vector_hits': 0, 'candidate_limit': MAX_ROWS}
    if not contract:
        return [], trace
    try:
        with closing(index._connect(timeout=0.01)) as db:
            query_row = db.execute('SELECT vector FROM semantic_queries WHERE query_hash = ? AND contract = ?', (digest(query), contract)).fetchone()
            if query_row is None:
                return [], {**trace, 'state': 'warming_query'}
            needle = vector(json.loads(query_row['vector']))
            rows = db.execute('SELECT c.*, s.vector, s.source_hash FROM semantic_chunks s JOIN chunks c ON c.id=s.chunk_id WHERE s.contract=? ORDER BY c.id LIMIT ?', (contract, MAX_ROWS)).fetchall()
        found = []
        for row in rows:
            if digest(row['body']) != row['source_hash']:
                continue
            held = vector(json.loads(row['vector']), len(needle))
            score = sum(a * b for a, b in zip(needle, held))
            if score < 0.25:
                continue
            found.append({'path': row['path'], 'heading': row['heading'], 'line': row['line'],
                          'snippet': ' '.join(row['body'].split())[:240], 'score': score})
        found.sort(key=lambda row: (-row['score'], row['path'], row['line']))
        return found[:limit], {**trace, 'vector_hits': len(found), 'state': 'ready'}
    except Exception:
        return [], {**trace, 'state': 'unavailable'}
