import json
from pathlib import Path

import pytest

from literature.models import Block, Document, TranslationEntry, TranslationLayer
from literature.storage import ProjectStore, StorageError, atomic_json
from literature.structure_edit import apply_edit, edit_history, move_history


@pytest.fixture
def project(tmp_path):
    source = tmp_path / 'book.pdf'
    source.write_bytes(b'%PDF-1.4')
    store = ProjectStore(tmp_path / 'library')
    pid = store.create_project(source, title='Book')['project_id']
    document = Document(project_id=pid, title='Book', source_language='en', blocks=[
        Block('a', 'title', 0, 0, 'Chapter 8', [10, 20, 30, 40], level=1),
        Block('b', 'paragraph', 1, 1, 'The Empire of Writing', [10, 20, 30, 40]),
        Block('c', 'paragraph', 2, 1, 'A paragraph.', [10, 50, 30, 70]),
    ])
    store.save_document(document)
    return store, pid


def test_merge_split_undo_redo_and_provenance(project):
    store, pid = project
    result = apply_edit(store, pid, {'operation': 'merge', 'block_ids': ['b', 'a'], 'separator': ' '})
    blocks = result['document']['blocks']
    assert blocks[0]['source_text'] == 'Chapter 8 The Empire of Writing'
    assert [source['page_index'] for source in blocks[0]['raw']['structure_edit_sources']] == [0, 1]
    assert result['can_undo'] and not result['can_redo']
    assert len(move_history(store, pid, 'undo')['document']['blocks']) == 3
    assert len(move_history(store, pid, 'redo')['document']['blocks']) == 2
    result = apply_edit(store, pid, {'operation': 'split', 'block_id': 'a', 'offset': 9})
    assert result['document']['blocks'][0]['source_text'] == 'Chapter 8'
    assert result['document']['blocks'][1]['block_id'] != 'a'
    assert not result['can_redo']
    move_history(store, pid, 'undo')
    move_history(store, pid, 'undo')
    assert len(store.load_document(pid).blocks) == 3


def test_merge_rejects_translations_and_annotations(project):
    store, pid = project
    layer = TranslationLayer(project_id=pid, locale='zh-CN', entries={'b': TranslationEntry(block_id='b', text='译文', locked=True)})
    store.save_translation(layer)
    with pytest.raises(StorageError, match='译文'):
        apply_edit(store, pid, {'operation': 'merge', 'block_ids': ['a', 'b'], 'separator': ' '})
    assert store.load_translation(pid).entries['b'].text == '译文'
    overlay = store.project_path(pid) / 'reader' / 'overlay.json'
    atomic_json(overlay, {'annotations': [{'id': 'n', 'block_id': 'a', 'start': 0, 'end': 2, 'note': 'note'}]})
    with pytest.raises(StorageError, match='批注'):
        apply_edit(store, pid, {'operation': 'edit_text', 'block_id': 'a', 'text': 'new'})
    assert store.load_document(pid).block('a').source_text == 'Chapter 8'
    assert not edit_history(store, pid)['can_undo']


def test_batch_history_conflict_and_no_partial(project):
    store, pid = project
    with pytest.raises(StorageError):
        apply_edit(store, pid, {'operation': 'set_levels', 'block_ids': ['a', 'b'], 'level': 2})
    assert store.load_document(pid).block('a').level == 1
    apply_edit(store, pid, {'operation': 'patch', 'block_id': 'b', 'patch_operation': 'set_title', 'after': 2})
    apply_edit(store, pid, {'operation': 'set_levels', 'block_ids': ['a', 'b'], 'level': 3})
    move_history(store, pid, 'undo')
    assert store.load_document(pid).block('b').level == 2
    move_history(store, pid, 'undo')
    assert store.load_document(pid).block('b').type == 'paragraph'
    document = store.load_document(pid)
    document.title = 'External change'
    store.save_document(document)
    with pytest.raises(StorageError, match='修改'):
        move_history(store, pid, 'redo')


def test_unicode_split_offset_and_interrupted_write_recovery(project, monkeypatch):
    store, pid = project
    apply_edit(store, pid, {'operation': 'edit_text', 'block_id': 'c', 'text': 'A😀B'})
    result = apply_edit(store, pid, {'operation': 'split', 'block_id': 'c', 'offset': 2})
    assert [b['source_text'] for b in result['document']['blocks'][-2:]] == ['A😀', 'B']
    original = store.save_document
    called = False

    def fail_once(document):
        nonlocal called
        if not called:
            called = True
            raise OSError('simulated interruption')
        return original(document)

    monkeypatch.setattr(store, 'save_document', fail_once)
    with pytest.raises(OSError):
        apply_edit(store, pid, {'operation': 'edit_text', 'block_id': 'a', 'text': 'new heading'})
    assert (store.project_path(pid) / 'document' / 'structure_edit_pending.json').exists()
    assert edit_history(store, pid)['can_undo']
    assert store.load_document(pid).block('a').source_text == 'new heading'
    assert not (store.project_path(pid) / 'document' / 'structure_edit_pending.json').exists()
    move_history(store, pid, 'undo')
    assert store.load_document(pid).block('a').source_text == 'Chapter 8'


def test_content_edit_blocked_while_translation_runs(project):
    store, pid = project
    job_id = store.create_job(pid, 'translate', {}, {})
    store.update_job(pid, job_id, status='running')
    with pytest.raises(StorageError, match='正在翻译'):
        apply_edit(store, pid, {'operation': 'edit_text', 'block_id': 'a', 'text': 'changed'})
    assert store.load_document(pid).block('a').source_text == 'Chapter 8'
