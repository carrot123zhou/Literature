"""Promote verified real-model fold results without generating them a second time.

Requires stopped client, identical facts/settings and unchanged derived baseline.
Copies only regenerable reader files; inserts new jobs/usage, never replaces the DB.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import datetime
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def replace_file(source, target):
    temp = target.with_name(f'.{target.name}.{uuid.uuid4().hex}.tmp')
    try:
        shutil.copy2(source, temp)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('result', type=Path)
    args = parser.parse_args()
    result = json.loads(args.result.read_text())
    assert result['after']['ready'] and result['cache_ready'] and result['cache_new_calls'] == 0
    assert result['facts_unchanged'] and not result['previous_valid_units_changed']
    base = Path(result['isolation'])
    copied = base / 'library' / result['book']
    production = ROOT / 'library' / result['book']
    runtime = Path.home() / 'Library/Application Support/Literature'
    commands = subprocess.check_output(['ps', '-axo', 'command='], text=True).splitlines()
    prefix = str(ROOT / 'launcher/Literature.app/Contents/MacOS') + '/'
    assert not any(line.strip().startswith(prefix) for line in commands), '请先正常退出 Literature'
    assert sha(runtime / 'model-services.json') == sha(base / 'runtime/model-services.json'), '模型设置已变化，未迁入'
    for name in ['document/document.json', 'translations/zh-CN/translation.json']:
        assert sha(production / name) == sha(copied / name), f'事实层已变化，未迁入：{name}'
    baseline = json.loads((base / 'baseline-derived-hashes.json').read_text())
    for name, expected in baseline.items():
        assert sha(production / 'reader' / name) == expected, f'原项目已产生新结果，未覆盖：{name}'
    pid = json.loads((production / 'project.json').read_text())['project_id']
    assert pid == json.loads((copied / 'project.json').read_text())['project_id']
    run = json.loads((copied / 'reader/fold_run.json').read_text())
    assert run['ready'] and run['binding'] == result['after']['binding']
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
    backup = ROOT / 'tmp' / 'fold-production-backups' / stamp / production.name
    backup.mkdir(parents=True)
    shutil.copytree(production / 'reader', backup / 'reader')
    with sqlite3.connect(f'file:{production / "literature.sqlite"}?mode=ro', uri=True) as source:
        with sqlite3.connect(backup / 'literature.sqlite') as dest:
            source.backup(dest)
    names = ['layers.json', 'native_source.json', 'native_zh-CN.json', 'fold_checkpoints.json', 'fold_usage.jsonl']
    try:
        for name in names:
            candidate = copied / 'reader' / name
            if candidate.exists():
                replace_file(candidate, production / 'reader' / name)
        inserted = {}
        with sqlite3.connect(production / 'literature.sqlite') as db:
            db.execute('ATTACH DATABASE ? AS verified', (str(copied / 'literature.sqlite'),))
            for table, key in [('jobs', 'job_id'), ('usage_records', 'usage_id')]:
                condition = " AND v.kind='reader_fold_run'" if table == 'jobs' else ""
                count = db.execute(f'SELECT count(*) FROM verified.{table} v WHERE NOT EXISTS '
                                   f'(SELECT 1 FROM main.{table} p WHERE p.{key}=v.{key})' + condition).fetchone()[0]
                db.execute(f'INSERT INTO main.{table} SELECT v.* FROM verified.{table} v WHERE NOT EXISTS '
                           f'(SELECT 1 FROM main.{table} p WHERE p.{key}=v.{key})' + condition)
                inserted[table] = count
        # Publish ready last, after payloads and usage are durable.
        from literature.reader import ReaderJobRunner, ReaderLayerService, ReaderStore
        from literature.storage import ProjectStore
        store = ProjectStore(ROOT / 'library')
        binding = run['binding']
        readiness = ReaderLayerService(store).fold_run_readiness(pid, locale=run['locale'],
                                                               service_id=binding['service_id'], model=binding['model'])
        assert readiness['ready'], readiness
        runner = ReaderJobRunner(store)
        run['ready_fingerprint'] = runner._run_fingerprint(pid, run)
        ReaderStore(store).save_fold_run(pid, run)
        assert runner.current_run(pid)['ready']
    except Exception:
        for name in names + ['fold_run.json']:
            original = backup / 'reader' / name
            destination = production / 'reader' / name
            if original.exists():
                replace_file(original, destination)
            else:
                destination.unlink(missing_ok=True)
        with sqlite3.connect(f'file:{backup / "literature.sqlite"}?mode=ro', uri=True) as original:
            with sqlite3.connect(production / 'literature.sqlite') as destination:
                original.backup(destination)
        print('验证产物迁入未完成，已恢复备份；原始文档和笔记未改动。', file=sys.stderr)
        raise
    report = {'book': result['book'], 'backup': str(backup), 'ready_sections': readiness['ready_sections'],
              'inserted': inserted, 'paid_calls_during_promotion': 0}
    (base / 'promoted.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
