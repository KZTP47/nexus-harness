"""Local HTTP ownership of email jobs and the bundled Kestra lifecycle.

Mail and preferences stay in the domain store. Kestra receives opaque draft IDs
and a dedicated callback capability; it never receives mailbox credentials.
"""
from __future__ import annotations

import secrets
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from .models import HarnessError

NOTICE_CONTRACT = 'email-notifications/v1'
NOTICE_SETTINGS_ID = 'setting:notifications'
NOTICE_DEFAULTS = {'enabled': True, 'show_details': True}
# A page that opens (or reloads) shortly after a notice was raised still shows
# it; mail found at startup is announced even if the panel took a moment.
NOTICE_REPLAY_SECONDS = 120
NOTICE_LIMIT = 50


def bundled_runtime() -> Path:
    source = Path(__file__).resolve()
    packaged = source.parents[3] / "kestra-runtime"
    development = source.parents[2] / "desktop" / "kestra-runtime"
    return packaged if packaged.is_dir() else development


class EmailService:
    def _public_error(self, error, operation=''):
        """Keep browser selectors, mailbox IDs and terminal traces out of the UI."""
        raw = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(error))
        if re.search(r'\b(?:locator|page)\.\w+|Call log:', raw):
            if operation.startswith('sync:'):
                return ('The inbox could not be read in time. Choose Check inbox now to retry. '
                        'Automatic checking will retry if enabled.')
            return 'The mailbox page did not finish the requested action. Check its status and reconnect if needed.'
        try:
            from .redaction import CredentialRedactor
            return CredentialRedactor(self.server.config).text(raw).split('\n', 1)[0][:1000]
        except Exception:
            return f'Email operation failed ({type(error).__name__}).'

    def __init__(self, server, *, studio=None, engine=None):
        self.server = server
        self.callback_token = secrets.token_urlsafe(32)
        self._studio = studio
        self._engine = engine
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._worker_active: dict[str, int] = {}
        self._stop = threading.Event()
        self._poller = None
        self._poll_due = {}
        self._poll_settings = {}
        self._scan_timing = {}
        self._onboarding = None
        # In-memory only: a restart gets a new boot mark so pages reset their
        # cursor instead of waiting for a sequence number that never comes.
        self._notice_boot = secrets.token_hex(8)
        self._notice_seq = 0
        self._notices = deque(maxlen=NOTICE_LIMIT)

    @property
    def onboarding(self):
        with self._lock:
            if self._onboarding is None:
                from .email_onboarding import EmailOnboarding
                self._onboarding = EmailOnboarding(self.studio)
            return self._onboarding

    @property
    def studio(self):
        with self._lock:
            if self._studio is None:
                from .email_studio import EmailStudio
                self._studio = EmailStudio(self.server.config)
            self._studio.config = self.server.config
            return self._studio

    @property
    def engine(self):
        with self._lock:
            if self._engine is None:
                from .email_kestra import KestraRuntime
                host = str(self.server.server_address[0])
                authority = f"[{host}]" if ":" in host else host
                self._engine = KestraRuntime(
                    bundled_runtime(),
                    self.server.config.project_root / ".harness" / "email-kestra",
                    f"http://{authority}:{self.server.server_port}",
                    self.callback_token,
                )
            return self._engine

    def _background(self, key, work):
        with self._lock:
            if self._stop.is_set():
                raise HarnessError("The email workspace is closing. Reopen it to continue.")
            if self._jobs.get(key, {}).get("state") == "running":
                return
            started = datetime.now(timezone.utc).isoformat()
            self._jobs[key] = {"id": key, "state": "running", "error": "",
                               "started_at": started, "updated_at": started, "finished_at": ""}
            # Keep completed diagnostic history bounded; active jobs remain owned.
            for old in list(self._jobs):
                if len(self._jobs) <= 100:
                    break
                if self._jobs[old]["state"] != "running":
                    del self._jobs[old]

        def run():
            try:
                work()
            except Exception as exc:
                error = self._public_error(exc, key)
                with self._lock:
                    finished = datetime.now(timezone.utc).isoformat()
                    self._jobs[key].update(state="failed", error=str(error)[:1000],
                                           updated_at=finished, finished_at=finished)
            else:
                with self._lock:
                    finished = datetime.now(timezone.utc).isoformat()
                    self._jobs[key].update(state="completed", updated_at=finished, finished_at=finished)

        threading.Thread(target=run, name="nexus-email-job", daemon=True).start()

    def _start_draft(self, draft):
        def work():
            try:
                # Poll and recovery can hold the same stale queued snapshot.
                # Recheck after taking job ownership, even if an earlier launch
                # already completed and its job is no longer marked running.
                current = self.studio._get('draft', draft['id'], draft['account_id'])
                if current.get('execution_id') or current['status'] not in {'queued', 'generating'}:
                    return
                self.engine.ensure_started()
                execution = self.engine.start_draft(draft["id"])
                self.studio.dispatch("bind_execution", {
                    "account_id": draft["account_id"], "draft_id": draft["id"],
                    "execution_id": execution,
                })
            except Exception as exc:
                self.studio.fail_draft(draft["id"], str(exc))
                raise
        self._background("draft:" + draft["id"], work)

    def _resume(self, draft):
        def work():
            self.engine.ensure_started()
            if not draft.get("execution_id"):
                raise HarnessError("This draft has no Kestra execution. Generate a new draft before approval.")
            execution_id = draft["execution_id"]
            state = self.engine.execution_status(execution_id).get("state", {}).get("current")
            if state in {"FAILED", "KILLED", "CANCELLED"}:
                # Stored approval remains authoritative. A replacement flow
                # still crosses the real Pause, and finalization is idempotent.
                replacement = self.engine.start_draft(draft["id"])
                self.studio.dispatch("rebind_execution", {
                    "account_id": draft["account_id"], "draft_id": draft["id"],
                    "previous_execution_id": execution_id, "execution_id": replacement,
                })
                execution_id = replacement
            self.engine.resume(execution_id)
        self._background("approve:" + draft["id"], work)

    def snapshot(self):
        try:
            oauth = self.onboarding.snapshot()
        except HarnessError:
            oauth = {name: {'configured': False, 'status': 'unavailable',
                            'message': 'Sign-in settings need repair in advanced setup. Manual setup and imported mail remain available.'}
                     for name in ('outlook', 'gmail')}
            oauth['pending'] = []
        result = self.studio.snapshot()
        result['local_connections'] = self.studio.local_mail.snapshot()
        result['oauth'] = oauth
        result['emailengine'] = self.studio.mail_backend.snapshot()
        try:
            result["orchestration"] = self.engine.status()
        except Exception as exc:
            result["orchestration"] = {"state": "unavailable", "error": str(exc)[:500]}
        with self._lock:
            result["operations"] = [dict(item) for item in self._jobs.values()]
            now = time.monotonic()
            result['polling'] = {'contract': 'email-polling/v1', 'accounts': [
                {'account_id': a['id'], 'interval_seconds': a.get('poll_seconds', 60),
                 'enabled': bool(a.get('poll_enabled')), 'scan_running': self._jobs.get('sync:' + a['id'], {}).get('state') == 'running',
                 'next_check_in_seconds': (None if not a.get('poll_enabled') or a.get('connection_state') in {'disconnected', 'reconnect_required'}
                                           or self._jobs.get('sync:' + a['id'], {}).get('state') == 'running'
                                           else max(0, round(self._poll_due.get(a['id'], now) - now, 2))),
                 **self._scan_timing.get(a['id'], {})} for a in result['accounts']]}
        result['notifications'] = self.notification_settings()
        result['captured_at'] = datetime.now(timezone.utc).isoformat()
        self._start_poller()
        return result

    def notification_settings(self):
        """The saved corner-notification choice, without creating a mail store."""
        existing = self.server.config.project_root / ".harness" / "email-studio" / "mail.sqlite3"
        if self._studio is None and not existing.is_file():
            return dict(NOTICE_DEFAULTS)
        try:
            saved = self.studio._get('setting', NOTICE_SETTINGS_ID)
        except HarnessError:
            return dict(NOTICE_DEFAULTS)
        return {key: bool(saved.get(key, value)) for key, value in NOTICE_DEFAULTS.items()}

    def _save_notification_settings(self, payload):
        chosen = self.notification_settings()
        for key in NOTICE_DEFAULTS:
            if key in payload:
                if not isinstance(payload[key], bool):
                    raise HarnessError('Notification settings must be on or off.')
                chosen[key] = payload[key]
        self.studio._put('setting', {'id': NOTICE_SETTINGS_ID, 'account_id': NOTICE_SETTINGS_ID,
                                     'contract': NOTICE_CONTRACT, **chosen})
        return {'notifications': chosen}

    def _announce_drafting(self, account, message, draft):
        """Queue a corner notification: new mail arrived and a reply is being drafted."""
        settings = self.notification_settings()
        if not settings['enabled']:
            return
        details = settings['show_details']
        name, address = parseaddr(str(message.get('sender', '')))
        with self._lock:
            self._notice_seq += 1
            self._notices.append({
                'seq': self._notice_seq, 'id': f'{self._notice_boot}-{self._notice_seq}', 'kind': 'drafting',
                'account_id': account['id'], 'message_id': message['id'], 'draft_id': draft['id'],
                'account': str(account.get('name') or account.get('email') or '')[:120] if details else '',
                'sender': (name or address or str(message.get('sender', '')))[:200] if details else '',
                'subject': str(message.get('subject', ''))[:300] if details else '',
                'private': not details,
                'created_at': datetime.now(timezone.utc).isoformat(), 'raised': time.monotonic(),
            })

    def notifications(self, after=None):
        """Bounded in-memory feed read by every open panel page, whatever tab it shows.

        The current settings apply to notices already in the feed: turning the
        cards off, or hiding sender and subject while sharing a screen, also
        covers a page that reloads and replays the last two minutes.
        """
        settings = self.notification_settings()
        with self._lock:
            now = time.monotonic()
            items = [{**{k: v for k, v in notice.items() if k != 'raised'},
                      'age_seconds': round(max(0.0, now - notice['raised']), 1)}
                     for notice in self._notices if after is None or notice['seq'] > after]
            if not settings['enabled']:
                items = []
            elif not settings['show_details']:
                items = [{**item, 'account': '', 'sender': '', 'subject': '', 'private': True} for item in items]
            return {'contract': NOTICE_CONTRACT, 'boot': self._notice_boot, 'seq': self._notice_seq,
                    'replay_seconds': NOTICE_REPLAY_SECONDS, 'items': items}

    def dispatch(self, action, payload):
        allowed = {"export_email", "engine_start", "refresh", "resume_draft", "prepare_draft", "sync",
                   "oauth_start", "oauth_auto_connect", "oauth_configure", "oauth_disconnect", "refresh_models",
                   "local_open", "local_mode", "local_discover", "local_status", "local_connect", "local_disconnect", "revise_draft",
                   "account_save", "import", "create_draft", "retry_draft", "retry_learning", "retry_automatic_learning",
                   "emailengine_configure", "emailengine_accounts", "emailengine_connect", "emailengine_prepare", "emailengine_sign_in", "check_delivery",
                   "save_draft", "approve_draft", "confirm_browser_delivery", "discard_draft", "memory_save", "memory_delete",
                   "notification_settings", "dismiss_failed_import"}
        if action not in allowed:
            raise HarnessError("Unknown email action.")
        if action == 'notification_settings':
            return self._save_notification_settings(payload)
        if action == 'emailengine_configure':
            return self.studio.mail_backend.configure(payload)
        if action == 'emailengine_prepare':
            self._background('mailbox-service', self.studio.mail_backend.prepare_local)
            return {'started': True}
        if action == 'emailengine_sign_in':
            host = str(self.server.server_address[0])
            authority = f'[{host}]' if ':' in host else host
            return self.studio.mail_backend.sign_in(f'http://{authority}:{self.server.server_port}/')
        if action == 'emailengine_accounts':
            return self.studio.mail_backend.accounts()
        if action == 'emailengine_connect':
            result = self.studio.connect_engine(payload)
            self._start_poller()
            return result
        if action == 'check_delivery':
            self._background('delivery:' + str(payload.get('draft_id', '')), lambda: self.studio.check_delivery(payload))
            return {'started': True}
        if action == 'local_open':
            return {'connection': self.studio.local_mail.open(payload.get('provider'), payload.get('connection_id', ''),
                                                             browser_mode=payload.get('browser_mode', 'headed')),
                    'message': 'Sign in in the browser window, then finish connecting here.'}
        if action == 'local_mode':
            return {'connection': self.studio.local_mail.configure_mode(payload.get('kind'), payload.get('connection_id'),
                                                                        payload.get('browser_mode'))}
        if action == 'local_discover':
            return {'connections': self.studio.local_mail.discover()}
        if action == 'local_status':
            return {'connection': self.studio.local_mail.status(payload.get('kind'), payload.get('connection_id'))}
        if action == 'local_connect':
            result = self.studio.connect_local(payload.get('kind'), payload.get('connection_id'), payload)
            self._start_poller()
            return result
        if action == 'local_disconnect':
            return self.studio.disconnect_local(payload.get('account_id'))
        if action == 'revise_draft':
            self._background('revise:' + str(payload.get('draft_id', '')), lambda: self.studio.revise_draft(payload))
            return {'started': True}
        if action == 'retry_automatic_learning':
            self._background('automatic-learning:' + str(payload.get('draft_id', '')), lambda: self.studio.retry_automatic_learning(payload))
            return {'started': True}
        if action == 'oauth_start':
            return self.onboarding.start(payload)
        if action == 'oauth_auto_connect':
            return self.onboarding.auto_connect(payload)
        if action == 'refresh_models':
            def refresh_models():
                from .email_models import model_options
                from .providers import ProviderRegistry
                for profile in ProviderRegistry(self.server.config).profiles():
                    if profile.name in ('claude-cli', 'codex-cli'):
                        model_options(self.server.config, profile.id, refresh=True)
            self._background('models', refresh_models)
            return {'started': True}
        if action == 'oauth_configure':
            return self.onboarding.configure(payload)
        if action == 'oauth_disconnect':
            return self.studio.disconnect_account(payload.get('account_id'))
        if action == "export_email":
            draft = next((d for d in self.studio.snapshot()["drafts"]
                          if d["id"] == payload.get("draft_id")
                          and d["account_id"] == payload.get("account_id")), None)
            if not draft or not draft.get("export_path"):
                raise HarnessError("This reply has not been exported yet.")
            path = Path(draft["export_path"]).resolve(strict=True)
            if not path.is_relative_to(self.studio.data_dir.resolve()) or path.suffix != ".eml":
                raise HarnessError("The saved reply is outside this mailbox store.")
            return {"filename": path.name, "content": path.read_text(encoding="utf-8")}
        if action == "engine_start":
            self._background("engine", self.engine.ensure_started)
            return {"started": True}
        if action == "refresh":
            return self.snapshot()
        if action == "prepare_draft":
            result = self.studio.prepare_draft(payload)
            # A successful readiness check supersedes terminal errors from a
            # previous attempt, without touching any active worker ownership.
            with self._lock:
                for prefix in ('approve:', 'finalize:', 'revise:'):
                    key = prefix + result['draft']['id']
                    if self._jobs.get(key, {}).get('state') == 'failed':
                        self._jobs.pop(key, None)
            return result
        if action == "resume_draft":
            draft = next((d for d in self.studio.snapshot()["drafts"]
                          if d["id"] == payload.get("draft_id")
                          and d["account_id"] == payload.get("account_id")), None)
            if not draft or draft["status"] != "approved":
                raise HarnessError("Only an already approved draft can resume its workflow.")
            self._resume(draft)
            return {"draft": draft}
        if action == "sync":
            account_id = str(payload.get("account_id", ""))
            self._background("sync:" + account_id, lambda: self.studio.dispatch("sync", payload))
            return {"started": True}
        if action == "retry_learning":
            # A model turn can take minutes; keep the editor responsive.
            self._background("learning:" + str(payload.get("draft_id", "")),
                             lambda: self.studio.dispatch(action, payload))
            return {"started": True}
        result = self.studio.dispatch(action, payload)
        if action == 'account_save' and result.get('account'):
            with self._lock:
                self._poll_due[result['account']['id']] = 0
                self._poll_settings.pop(result['account']['id'], None)
        if action in {"create_draft", "retry_draft"}:
            if result["draft"]["status"] == "queued" and not result["draft"].get("execution_id"):
                self._start_draft(result["draft"])
        elif action == "approve_draft":
            self._resume(result["draft"])
        return result

    def worker(self, stage, payload):
        draft_id = payload.get("draft_id")
        if not isinstance(draft_id, str) or not draft_id or len(draft_id) > 128:
            raise HarnessError("A valid draft ID is required.")
        if stage not in {"generate", "finalize"}:
            raise HarnessError("Unknown email workflow stage.")
        # Workflow submission/resume completion is not AI or send completion.
        # These rows reflect the actual authenticated callback lifetime.
        key = stage + ':' + draft_id
        with self._lock:
            if not self._worker_active.get(key):
                started = datetime.now(timezone.utc).isoformat()
                self._jobs[key] = {"id": key, "state": "running", "error": "",
                                   "started_at": started, "updated_at": started, "finished_at": ""}
            self._worker_active[key] = self._worker_active.get(key, 0) + 1
        error = ''
        try:
            result = self.studio.process_draft(draft_id) if stage == 'generate' else self.studio.finalize_draft(draft_id)
        except Exception as exc:
            error = self._public_error(exc, key)
            raise
        finally:
            with self._lock:
                remaining = self._worker_active[key] - 1
                if error:
                    self._jobs[key]['error'] = error
                if remaining:
                    self._worker_active[key] = remaining
                else:
                    self._worker_active.pop(key, None)
                    finished = datetime.now(timezone.utc).isoformat()
                    self._jobs[key].update(state='failed' if self._jobs[key]['error'] else 'completed',
                                           updated_at=finished, finished_at=finished)
        # Kestra persists HTTP output. Keep correspondence out of that output.
        return {"draft_id": draft_id, "status": result["draft"]["status"]}

    def _start_poller(self):
        with self._lock:
            if self._poller is not None or self._stop.is_set():
                return
            self._poller = threading.Thread(target=self._poll, name="nexus-email-inbox", daemon=True)
            self._poller.start()

    def _poll(self):
        maintenance_due = 0
        while not self._stop.wait(.5):
            try:
                self._schedule_polls(self.studio.polling_accounts())
                if time.monotonic() >= maintenance_due:
                    maintenance_due = time.monotonic() + 5
                    self._background('maintenance:email', self._poll_maintenance)
            except Exception:
                # Per-account errors are recorded by the owned background job.
                continue

    def _poll_maintenance(self):
        if self._onboarding is not None:
            try:
                self._onboarding.snapshot()
            except HarnessError:
                # Broken sign-in setup must not stop manual mailbox
                # polling or recovery of existing review workflows.
                pass
        state = self.studio.snapshot()
        pending = [d for d in state["drafts"] if d["status"] in {"queued", "generating"}]
        for draft in pending:
            with self._lock:
                active = self._jobs.get("draft:" + draft["id"], {}).get("state") == "running"
            if active:
                continue
            if not draft.get("execution_id"):
                self._start_draft(draft)
            else:
                def reconcile(item=draft):
                    execution = self.engine.execution_status(item["execution_id"])
                    if execution.get("state", {}).get("current") in {"FAILED", "KILLED", "CANCELLED"}:
                        self.studio.fail_draft(item["id"], "The workflow was interrupted. Retry draft generation to continue.")
                self._background("recovery:" + draft["id"], reconcile)

    def _schedule_polls(self, accounts):
        from .email_local import LOCAL_KINDS
        for account in accounts:
            key = account['id']
            interval = max(1, int(account.get('poll_seconds', 60)))
            settings = (interval, bool(account.get('poll_enabled')), account.get('connection_state'))
            with self._lock:
                if self._poll_settings.get(key) != settings:
                    self._poll_settings[key] = settings
                    self._poll_due[key] = 0
                if (not account.get('poll_enabled') or account.get('kind') not in {'imap', 'outlook', 'gmail', 'emailengine'} | LOCAL_KINDS
                        or account.get('connection_state') in {'disconnected', 'reconnect_required'}):
                    continue
                if self._jobs.get('sync:' + key, {}).get('state') == 'running':
                    continue
                if time.monotonic() < self._poll_due.get(key, 0):
                    continue
                self._poll_due[key] = time.monotonic() + interval
                self._background('sync:' + key, lambda account_id=key: self._poll_account(account_id))

    def _poll_account(self, account_id):
        started = time.monotonic()
        with self._lock:
            self._scan_timing[account_id] = {**self._scan_timing.get(account_id, {}),
                'last_started_at': datetime.now(timezone.utc).isoformat()}
        try:
            return self._scan_account(account_id)
        finally:
            with self._lock:
                self._scan_timing[account_id].update(last_finished_at=datetime.now(timezone.utc).isoformat(),
                    last_duration_seconds=round(time.monotonic() - started, 3))

    def _scan_account(self, account_id):
        # Only mail this scan imported is new. Older stored mail that is drafted
        # now (after a restart, or a manual check) is never announced as new.
        scan_started = datetime.now(timezone.utc)
        for _ in range(8):
            if self._stop.is_set():
                return
            result = self.studio.dispatch('sync', {'account_id': account_id})
            if not result.get('has_more'):
                break
        # Import backlog takes priority over preparing replies. A slow or failed
        # draft workflow must not hold the next batch of inbox messages behind
        # every already-imported message. The scheduler resumes this cursor on
        # its next tick; draft preparation starts after the backlog is drained.
        if result.get('has_more'):
            return
        current = self.studio.snapshot()
        owning = next(a for a in current['accounts'] if a['id'] == account_id)
        if not owning.get('poll_enabled') or owning.get('connection_state') == 'disconnected':
            return
        drafted = {d['message_id'] for d in current['drafts']}
        for message in current['messages']:
            if (self._stop.is_set()):
                return
            if (message['account_id'] == account_id and message['id'] not in drafted
                    and message.get('auto_draft_eligible', True)
                    and message.get('account_fingerprint') == owning.get('fingerprint')):
                result = self.studio.dispatch('create_draft', {'account_id': account_id, 'message_id': message['id']})
                if (result['draft'].get('status') == 'queued' and not result['draft'].get('revision')
                        and self._imported_since(message, scan_started)):
                    try:
                        self._announce_drafting(owning, message, result['draft'])
                    except Exception:
                        pass  # A notification must never stop the draft it announces.
                self._start_draft(result['draft'])

    @staticmethod
    def _imported_since(message, moment):
        try:
            imported = datetime.fromisoformat(str(message.get('imported_at', '')))
        except ValueError:
            return False
        return (imported if imported.tzinfo else imported.replace(tzinfo=timezone.utc)) >= moment

    def close(self):
        self._stop.set()
        if self._engine is not None:
            self._engine.close()
        if self._studio is not None and getattr(self._studio, '_connectors', None) is not None:
            self._studio._connectors.close()
        if self._studio is not None:
            self._studio.mail_backend.close()
            self._studio.local_mail.close()

    def restore_if_present(self):
        """Resume opted-in inbox polling without requiring the tab to be open."""
        existing = self.server.config.project_root / ".harness" / "email-studio" / "mail.sqlite3"
        if existing.is_file():
            self._start_poller()
