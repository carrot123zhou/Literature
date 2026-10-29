"""Real AIFold recovery via production API in a disposable library.

Reads the configured Keychain credential, never edits production settings/books.
Run only explicitly for real-service acceptance (not collected by pytest).
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--book', choices=['beyond', 'imperiled'], required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--max-new-calls', type=int, default=0)
    args = parser.parse_args()
    base = Path(args.output).resolve()
    base.mkdir(parents=True, exist_ok=True)
    runtime = base / 'runtime'
    library = base / 'library'
    runtime.mkdir(exist_ok=True)
    library.mkdir(exist_ok=True)
    production = Path.home() / 'Library/Application Support/Literature'
    for name in ['model-services.json', 'thinking-policy.json']:
        shutil.copyfile(production / name, runtime / name)
    pattern = 'THE BEYOND*' if args.book == 'beyond' else 'Imperiled*'
    source = next((ROOT / 'library').glob(pattern))
    target = library / source.name
    if not target.exists():
        target.mkdir()
        shutil.copyfile(source / 'project.json', target / 'project.json')
        for folder in ['document', 'translations', 'reader']:
            shutil.copytree(source / folder, target / folder)
        with sqlite3.connect(f'file:{source / "literature.sqlite"}?mode=ro', uri=True) as src:
            with sqlite3.connect(target / 'literature.sqlite') as dst:
                src.backup(dst)
    fact_paths = [source / 'document/document.json', source / 'translations/zh-CN/translation.json',
                  source / 'ocr/raw/manifest.json', production / 'model-services.json']
    before = {str(p): digest(p) for p in fact_paths if p.exists()}
    (base / 'facts-before.json').write_text(json.dumps(before, indent=2))
    baseline_path = base / 'baseline-derived-hashes.json'
    if not baseline_path.exists():
        names = ['layers.json', 'native_source.json', 'native_zh-CN.json', 'fold_checkpoints.json']
        baseline_path.write_text(json.dumps({name: digest(target / 'reader' / name)
                                            if (target / 'reader' / name).exists() else None
                                            for name in names}, indent=2))
    if args.prepare_only:
        print(json.dumps({'prepared': str(base), 'book': source.name}, ensure_ascii=False))
        return 0
    os.environ.update(LITERATURE_RUNTIME=str(runtime), LITERATURE_LIBRARY=str(library),
                      LITERATURE_SESSION_TOKEN='fold-real-isolated')
    from fastapi.testclient import TestClient
    from literature.api import create_app
    from literature.storage import ProjectStore
    store = ProjectStore(library)
    pid = json.loads((target / 'project.json').read_text())['project_id']
    baseline_hashes = json.loads(baseline_path.read_text())
    assert digest(source / 'reader/layers.json') == baseline_hashes['layers.json'], '原项目基线已变化'
    old_layers = json.loads((source / 'reader/layers.json').read_text())
    fields = ['takeaway', 'key', 'topic', 'brief', 'evidence']
    old_source = {(section['id'], unit['id']): unit
                  for section in old_layers['docs'].get('source', {}).get('sections', [])
                  for unit in section.get('units', [])}
    valid_before = {(locale, section['id'], unit['id']): {key: unit.get(key) for key in fields}
                    for locale, doc in old_layers['docs'].items()
                    for section in doc.get('sections', []) for unit in section.get('units', [])
                    if unit.get('source') == 'model' and (locale == 'source' or
                       old_source.get((section['id'], unit['id']), {}).get('source') == 'model')}
    clone_facts = [target / 'document/document.json', target / 'translations/zh-CN/translation.json']
    before.update({str(path): digest(path) for path in clone_facts})
    result = {'book': source.name, 'isolation': str(base), 'snapshots': []}
    from literature.reader import ReaderLayerService
    original_call = ReaderLayerService._call_model
    def traced_call(self, *call_args, **kwargs):
        outcome = original_call(self, *call_args, **kwargs)
        if kwargs.get('section_id') == 'S49':
            with (base / 'S49-replies.jsonl').open('a') as trace:
                trace.write(json.dumps({'stage': kwargs.get('stage'), 'payload': outcome.payload,
                                        'error': outcome.error}, ensure_ascii=False) + '\n')
        return outcome
    ReaderLayerService._call_model = traced_call
    app = create_app(store)
    started = time.monotonic()
    with TestClient(app, headers={'x-literature-token': 'fold-real-isolated'}) as client:
        assert app.state.job_manager.models.path.resolve().is_relative_to(runtime)
        existing = client.get(f'/projects/{pid}/reader/fold-run').json()
        result['before'] = existing
        stamp = time.strftime('%Y%m%d-%H%M%S')
        response = client.post(f'/projects/{pid}/reader/fold-runs', json={'locale': 'zh-CN'})
        response.raise_for_status()
        active = response.json()['run']
        job_id = active['job_id']
        initial_usage_ids = {row['usage_id'] for row in store.usage_records(pid)}
        last_activity = None
        while time.monotonic() - started < 1800:
            run = client.get(f'/projects/{pid}/reader/fold-run').json()
            new_call_count = sum(row['usage_id'] not in initial_usage_ids for row in store.usage_records(pid))
            budget_pause = args.max_new_calls > 0 and new_call_count >= args.max_new_calls
            if ((base / 'pause-requested').exists() or budget_pause) and run.get('status') == 'running':
                client.post(f'/projects/{pid}/reader/fold-runs/{run["run_id"]}/pause').raise_for_status()
            key = (run.get('status'), run.get('progress'), run.get('activity'))
            if key != last_activity:
                result['snapshots'].append(run)
                (base / 'in-progress.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
                print(json.dumps({'status': run.get('status'), 'progress': run.get('progress'),
                                  'activity': run.get('activity')}, ensure_ascii=False), flush=True)
                last_activity = key
            if run.get('status') not in ['running', 'pausing']:
                break
            time.sleep(2)
        else:
            client.post(f'/projects/{pid}/reader/fold-runs/{run["run_id"]}/pause')
            raise RuntimeError('隔离整书验收超过30分钟，已请求边界暂停')
        result['after'] = run
        result['elapsed_seconds'] = round(time.monotonic() - started, 2)
        with sqlite3.connect(f'file:{source / "literature.sqlite"}?mode=ro', uri=True) as original_db:
            baseline_ids = {row[0] for row in original_db.execute('SELECT usage_id FROM usage_records')}
        records = [row for row in store.usage_records(pid) if row['usage_id'] not in baseline_ids]
        result['current_job_calls'] = sum(row['job_id'] == job_id for row in records)
        result['usage_scope'] = 'all isolated acceptance calls absent from unchanged production baseline'
        result['usage'] = records
        result['tokens'] = {key: sum(row[key] for row in records) for key in ['input_tokens', 'output_tokens', 'reasoning_tokens']}
        result['calls'] = len(records)
        if run.get('ready'):
            count = len(store.usage_records(pid))
            again = client.post(f'/projects/{pid}/reader/fold-runs', json={'locale': 'zh-CN'})
            again.raise_for_status()
            for _ in range(60):
                cached = client.get(f'/projects/{pid}/reader/fold-run').json()
                if cached.get('status') not in ['running', 'pausing']:
                    break
                time.sleep(0.5)
            result['cache_ready'] = cached.get('ready')
            result['cache_new_calls'] = len(store.usage_records(pid)) - count
    new_layers = json.loads((target / 'reader/layers.json').read_text())
    after_units = {(locale, section['id'], unit['id']): unit
                   for locale, doc in new_layers['docs'].items()
                   for section in doc.get('sections', []) for unit in section.get('units', [])}
    changed = [key for key, value in valid_before.items()
               if value != {field: after_units.get(key, {}).get(field) for field in fields}]
    # Approved back-matter policy intentionally replaces old short layers with Full-only.
    # Exempt only an AI-confirmed, receipt-bound source section; body text stays exact.
    checkpoint_data = json.loads((target / 'reader/fold_checkpoints.json').read_text())
    accepted = []
    for key in list(changed):
        locale, sid, uid = key
        unit = after_units.get(key, {})
        source_unit = after_units.get(('source', sid, uid), {})
        receipt = checkpoint_data.get('sections', {}).get('source', {}).get(sid, {}).get('apparatus', {})
        binding = result['after']['binding']
        if (unit.get('source') == 'suppressed' and source_unit.get('suppress_reason') == 'apparatus'
                and receipt.get('affirmative') is True and receipt.get('mixed') is False
                and receipt.get('kind') in ['index', 'bibliography', 'catalog']
                and receipt.get('service_id') == binding['service_id']
                and receipt.get('model') == binding['model'] and result['after'].get('ready')):
            accepted.append(key)
            changed.remove(key)
    result['approved_apparatus_units_changed'] = accepted
    result['previous_valid_units_changed'] = changed
    result['facts_unchanged'] = all(digest(Path(path)) == value for path, value in before.items())
    output = base / f'result-{stamp}.json'
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({'output': str(output), **{k: result.get(k) for k in ['elapsed_seconds','calls','tokens','facts_unchanged','cache_ready','cache_new_calls']}}, ensure_ascii=False))
    return 0 if result['after'].get('ready') and result['facts_unchanged'] and result.get('cache_new_calls') == 0 and not changed else 1


if __name__ == '__main__':
    raise SystemExit(main())
