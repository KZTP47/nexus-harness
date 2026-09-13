"""Explicit opt-in live Claude/Codex email acceptance using synthetic mail only."""
from __future__ import annotations
import argparse
import copy
import json
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from our_harness.config import DEFAULT_CONFIG, LoadedConfig, load_config
from our_harness.email_studio import EmailStudio
from our_harness.providers import ProviderRegistry


def run_route(source, route, model=''):
    started = time.monotonic()
    receipt = {'route': route, 'model': model or 'configured', 'state': 'running', 'real_mail_sent': False}
    try:
        with tempfile.TemporaryDirectory(prefix='nexus-synthetic-mail-') as temporary:
            data = copy.deepcopy(DEFAULT_CONFIG)
            data['provider'] = copy.deepcopy(source.data['provider'])
            data['providers'] = copy.deepcopy(source.data.get('providers', {}))
            config = LoadedConfig(data, Path(temporary), [], {})
            studio = EmailStudio(config)
            account = studio.dispatch('account_save', {'name': 'Synthetic acceptance', 'email': 'writer@example.test', 'kind': 'import', 'provider_route': route, 'provider_model': model})['account']
            incoming = studio.dispatch('import', {'account_id': account['id'], 'sender': 'Colleague <colleague@example.test>', 'subject': 'Synthetic project update', 'body': 'Hello! Could you explain what information you need to prepare a project status update? This is synthetic test mail.'})['message']
            draft = studio.dispatch('create_draft', {'account_id': account['id'], 'message_id': incoming['id']})['draft']
            draft = studio.process_draft(draft['id'])['draft']
            receipt['generated'] = draft['status'] == 'review' and bool(draft['original'].strip())
            print(json.dumps({'route': route, 'stage': 'first_draft', 'success': receipt['generated']}), flush=True)
            draft = studio.revise_draft({'account_id': account['id'], 'draft_id': draft['id'],
                'revision': draft['revision'], 'text': draft['edited'],
                'instruction': 'Make this shorter, keeping the request for information. Do not add any commitments.'})['draft']
            receipt['ai_revision'] = draft['status'] == 'review' and bool(draft['edited'].strip()) and draft['revision'] == 3
            # Explicit feedback is expressed by changing the reply's signoff.
            edited = 'Hi,\n\nPlease share the current milestones, blockers, and next steps so I can prepare the update.\n\nWarm wishes,\nMorgan'
            studio.dispatch('approve_draft', {'account_id': account['id'], 'draft_id': draft['id'], 'revision': draft['revision'], 'text': edited, 'learn': True})
            final = studio.finalize_draft(draft['id'])['draft']
            memories = studio.snapshot()['memories']
            receipt.update(exported=final['status'] == 'exported' and Path(final['export_path']).is_file(), learning_complete=bool(final.get('learning_complete')), learned_rules=[m['text'] for m in memories])
            print(json.dumps({'route': route, 'stage': 'learned', 'success': receipt['learning_complete'], 'rules': receipt['learned_rules']}), flush=True)
            studio = EmailStudio(config)
            next_mail = studio.dispatch('import', {'account_id': account['id'], 'sender': 'Colleague <colleague@example.test>', 'subject': 'Another synthetic update', 'body': 'Could you tell me what information to collect for our next project update?'})['message']
            next_draft = studio.dispatch('create_draft', {'account_id': account['id'], 'message_id': next_mail['id']})['draft']
            next_draft = studio.process_draft(next_draft['id'])['draft']
            receipt.update(second_generated=next_draft['status'] == 'review', second_draft=next_draft['original'], learned_signoff_used='warm wishes' in next_draft['original'].lower(), persisted_rules=len(studio.snapshot()['memories']))
            receipt['state'] = 'passed' if all(receipt.get(k) for k in ('generated', 'ai_revision', 'exported', 'learning_complete', 'second_generated', 'learned_signoff_used', 'persisted_rules')) else 'failed'
    except Exception as exc:
        # Domain errors are already redacted/generic; never dump provider output or auth files.
        receipt.update(state='failed', error=str(exc)[:500])
    receipt['elapsed_seconds'] = round(time.monotonic() - started, 2)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--route', action='append', default=[])
    parser.add_argument('--model', default='', help='Explicit model override for the selected route.')
    parser.add_argument('--live', action='store_true', help='Explicitly authorize subscription-backed live requests.')
    args = parser.parse_args()
    if not args.live:
        parser.error('--live is required; this test consumes provider subscription usage')
    source = load_config(args.project)
    routes = [p.id for p in ProviderRegistry(source).profiles() if p.name in ('claude-cli', 'codex-cli') and (not args.route or p.id in args.route)]
    if not routes:
        parser.error('No configured Claude/Codex route matched.')
    with ThreadPoolExecutor(max_workers=min(2, len(routes))) as pool:
        results = list(pool.map(lambda route: run_route(source, route, args.model), routes))
    print(json.dumps({'email_provider_acceptance': results}, indent=2), flush=True)
    return 0 if all(r['state'] == 'passed' for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
