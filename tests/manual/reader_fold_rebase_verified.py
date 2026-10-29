"""Merge a verified fold result with newer production results, in isolation only."""
from pathlib import Path
import copy
import json
import os
import sqlite3
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    prior = Path(sys.argv[1]).resolve()
    fresh = Path(sys.argv[2]).resolve()
    reports = [json.loads(p.read_text()) for p in sorted(prior.glob('result-*.json'), reverse=True)]
    report = next(r for r in reports if r.get('after', {}).get('ready')
                  and r.get('cache_ready') and r.get('cache_new_calls') == 0
                  and r.get('facts_unchanged') and not r.get('previous_valid_units_changed'))
    name = report['book']
    original = ROOT / 'library' / name
    settings = Path.home() / 'Library/Application Support/Literature/model-services.json'
    assert settings.read_bytes() == (prior / 'runtime/model-services.json').read_bytes()
    target = fresh / 'library' / name
    proven = prior / 'library' / name
    for relative in ['document/document.json', 'translations/zh-CN/translation.json']:
        assert original.joinpath(relative).read_bytes() == proven.joinpath(relative).read_bytes()
        assert target.joinpath(relative).read_bytes() == original.joinpath(relative).read_bytes()
    old = json.loads((target / 'reader/layers.json').read_text())
    verified = json.loads((proven / 'reader/layers.json').read_text())
    merged = copy.deepcopy(verified)
    index = lambda payload, locale: {s['id']: s for s in payload['docs'][locale]['sections']}
    osrc, vsrc = index(old, 'source'), index(verified, 'source')
    old_targets = index(old, 'zh-CN')
    identity = lambda u: [u.get(k) for k in ['takeaway', 'key', 'topic', 'evidence']]
    chosen_source = {}
    counts = {'newer_source_kept': 0, 'newer_target_kept': 0, 'verified_target_used': 0, 'pending': 0}
    for sec in merged['docs']['source']['sections']:
        current = osrc.get(sec['id'], {})
        current_units = {u['id']: u for u in current.get('units', [])}
        for i, unit in enumerate(sec['units']):
            latest = current_units.get(unit['id'])
            if (unit.get('suppress_reason') != 'apparatus' and latest
                    and latest.get('source') == 'model' and latest.get('full') == unit.get('full')):
                sec['units'][i] = copy.deepcopy(latest)
                counts['newer_source_kept'] += 1
        if current.get('takeaway'):
            for key in ['takeaway', 'topic']:
                sec[key] = current.get(key, sec.get(key))
        chosen_source[sec['id']] = {u['id']: u for u in sec['units']}
    for sec in merged['docs']['zh-CN']['sections']:
        current = old_targets.get(sec['id'], {})
        current_units = {u['id']: u for u in current.get('units', [])}
        previous_source = {u['id']: u for u in osrc.get(sec['id'], {}).get('units', [])}
        verified_source = {u['id']: u for u in vsrc[sec['id']]['units']}
        for i, unit in enumerate(sec['units']):
            chosen = chosen_source[sec['id']][unit['id']]
            latest = current_units.get(unit['id'])
            if chosen.get('suppress_reason') == 'apparatus':
                continue
            if (latest and latest.get('source') == 'model'
                    and previous_source.get(unit['id'], {}).get('source') == 'model'
                    and identity(previous_source[unit['id']]) == identity(chosen)
                    and latest.get('full') == unit.get('full')):
                sec['units'][i] = copy.deepcopy(latest)
                counts['newer_target_kept'] += 1
            elif identity(verified_source[unit['id']]) == identity(chosen):
                counts['verified_target_used'] += 1
            else:
                sec['units'][i] = {**unit, 'source': 'pending', 'takeaway': '', 'key': '',
                                   'topic': '', 'brief': [], 'evidence': []}
                counts['pending'] += 1
        if current.get('takeaway'):
            for key in ['takeaway', 'topic']:
                sec[key] = current.get(key, sec.get(key))
    os.environ['LITERATURE_RUNTIME'] = str(fresh / 'runtime')
    os.environ['LITERATURE_LIBRARY'] = str(fresh / 'library')
    from literature.storage import ProjectStore
    from literature.reader import ReaderLayerService, ReaderStore, _source_fold_fingerprint, section_digest, derive_section_digest
    from literature.textfold_adapter import build_ladder_doc, _glossary_hash
    from literature.reader import _sections_from_ladder
    store = ProjectStore(fresh / 'library')
    pid = json.loads((target / 'project.json').read_text())['project_id']
    service = ReaderLayerService(store)
    binding = json.loads((proven / 'reader/fold_run.json').read_text())['binding']
    source_sections = {s['id']:s for s in _sections_from_ladder(build_ladder_doc(store.load_document(pid)))}
    source_map = index(merged, 'source')
    glossary = _glossary_hash(store.load_translation(pid, 'zh-CN'))
    for locale in ['source', 'zh-CN']:
        merged['docs'][locale]['failed'] = []
        for sec in merged['docs'][locale]['sections']:
            canonical = section_digest(source_sections[sec['id']], 'source', **{k:binding[k] for k in ['service_id','model']})
            has_model = any(u.get('source') == 'model' for u in sec['units'])
            if not has_model:
                preview = (source_sections[sec['id']] if locale == 'source' else service._document_context(pid, locale, store.load_translation(pid, locale))[2])
                if isinstance(preview, list): preview = next(s for s in preview if s['id'] == sec['id'])
                digest = service._local_digest(preview, locale)
            elif locale == 'source':
                digest = canonical
            else:
                digest = derive_section_digest(locale=locale, source_digest=canonical, glossary_hash=glossary,
                                               source_fingerprint=_source_fold_fingerprint(source_map[sec['id']]),
                                               **{k:binding[k] for k in ['service_id','model']})
            merged['section_hashes'][locale][sec['id']] = digest
    ReaderStore(store).save_layers(pid, merged)
    # Keep classifier receipts. Unit checkpoints with mismatched consumption fingerprints
    # remain safe misses; successful cache rehydration uses the preserved section projections.
    import shutil
    shutil.copyfile(proven / 'reader/fold_checkpoints.json', target / 'reader/fold_checkpoints.json')
    for native in target.glob('reader/native_*.json'): native.unlink()
    with sqlite3.connect(target / 'literature.sqlite') as db:
        db.execute('ATTACH DATABASE ? AS verified', (str(proven / 'literature.sqlite'),))
        for table, key in [('jobs','job_id'),('usage_records','usage_id')]:
            db.execute(f'INSERT INTO main.{table} SELECT v.* FROM verified.{table} v WHERE NOT EXISTS '
                       f'(SELECT 1 FROM main.{table} p WHERE p.{key}=v.{key})')
    (fresh / 'rebase.json').write_text(json.dumps(counts, indent=2))
    print(json.dumps(counts))

if __name__ == '__main__': main()
