"""用户发起的规范文档编辑及有冲突检测的撤销、重做。"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import asdict
from typing import Any

from .mineru import build_toc, write_source_markdown
from .models import Block, Document, stable_json
from .storage import ProjectStore, StorageError, atomic_json
from .reader import ReaderStore, _path_lock
from .structure import _prepare_patch


def _fingerprint(value: dict[str, Any]) -> str:
    value = copy.deepcopy(value)
    value.pop('updated_at', None)
    return hashlib.sha256(stable_json(value).encode()).hexdigest()


def _history_path(store: ProjectStore, project_id: str):
    return store.project_path(project_id) / 'document' / 'structure_edit_history.json'


def _history(store: ProjectStore, project_id: str) -> dict[str, Any]:
    path = _history_path(store, project_id)
    if not path.exists():
        return {'undo': [], 'redo': []}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or not isinstance(value.get('undo'), list) or not isinstance(value.get('redo'), list):
            raise ValueError('invalid history')
        return value
    except (ValueError, OSError) as error:
        raise StorageError('结构编辑历史无法读取，请保留文件并检查。') from error


def _recover(store: ProjectStore, project_id: str) -> None:
    path = store.project_path(project_id) / 'document' / 'structure_edit_pending.json'
    if not path.exists():
        return
    try:
        pending = json.loads(path.read_text(encoding='utf-8'))
        current = _fingerprint(store.load_document(project_id).to_dict())
        if current not in {pending['before_hash'], pending['after_hash']}:
            raise StorageError('结构编辑中断后文档又被修改，无法安全恢复，请保留项目文件并检查。')
        document = Document.from_dict(pending['document'])
        if _fingerprint(document.to_dict()) != pending['after_hash']:
            raise StorageError('结构编辑恢复记录校验失败，请保留项目文件并检查。')
        store.save_document(document)
        write_source_markdown(store.project_path(project_id), document)
        atomic_json(_history_path(store, project_id), pending['history'])
        path.unlink()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise StorageError('结构编辑恢复记录无法读取，请保留项目文件并检查。') from error


def recover_edit(store: ProjectStore, project_id: str) -> None:
    with store.document_lock(project_id):
        _recover(store, project_id)


def _commit(store: ProjectStore, project_id: str, before_hash: str, document: Document, history: dict[str, Any]) -> None:
    path = store.project_path(project_id) / 'document' / 'structure_edit_pending.json'
    atomic_json(path, {'before_hash': before_hash, 'after_hash': _fingerprint(document.to_dict()), 'document': document.to_dict(), 'history': history})
    store.save_document(document)
    write_source_markdown(store.project_path(project_id), document)
    atomic_json(_history_path(store, project_id), history)
    path.unlink()


def edit_history(store: ProjectStore, project_id: str) -> dict[str, bool]:
    with store.document_lock(project_id):
        _recover(store, project_id)
        history = _history(store, project_id)
        current = _fingerprint(store.load_document(project_id).to_dict())
        return {'can_undo': bool(history['undo'] and history['undo'][-1]['after_hash'] == current),
                'can_redo': bool(history['redo'] and history['redo'][-1]['before_hash'] == current)}


def _simple(block: Block) -> None:
    if block.type not in {'title', 'paragraph'} or block.cells or block.items or block.caption or block.footnote or block.resource_refs:
        raise StorageError('只能编辑普通文本段落或标题；包含表格、图片或附属内容的块不能合并、拆分或改写。')


def _loc(block: Block) -> list[dict[str, Any]]:
    locations = block.raw.get('structure_edit_sources')
    if isinstance(locations, list):
        return copy.deepcopy(locations)
    return [{'block_id': block.block_id, 'page_index': block.page_index, 'bbox': copy.deepcopy(block.bbox), 'source_text': block.source_text}]


def _perform(document: Document, request: dict[str, Any]) -> None:
    operation = request.get('operation')
    if operation in {'patch', 'patch_batch'}:
        ids = [request.get('block_id')] if operation == 'patch' else (request.get('block_ids') or [])
        if not ids or len(ids) != len(set(ids)):
            raise StorageError('请选择有效的内容块。')
        for block_id in ids:
            _prepare_patch(document, {'block_id': block_id, 'operation': request.get('patch_operation'), 'after': request.get('after')}, 'user')
    elif operation == 'set_levels':
        ids = request.get('block_ids') or []
        level = request.get('level')
        if not ids or len(ids) != len(set(ids)) or isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 6:
            raise StorageError('请选择标题并指定 1 至 6 级。')
        for block_id in ids:
            block = document.block(block_id)
            if block.type != 'title':
                raise StorageError('只能批量调整标题层级。')
            block.level = level
            block.structure_revision += 1
    elif operation == 'merge':
        ids = request.get('block_ids') or []
        if len(ids) < 2 or len(ids) != len(set(ids)):
            raise StorageError('请选择至少两个不同的相邻块。')
        positions = sorted(i for i, block in enumerate(document.blocks) if block.block_id in ids)
        if len(positions) != len(ids) or positions != list(range(positions[0], positions[-1] + 1)):
            raise StorageError('请选择阅读顺序中连续的块。')
        blocks = [document.blocks[i] for i in positions]
        for block in blocks:
            _simple(block)
        separator = request.get('separator')
        if not isinstance(separator, str) or len(separator) > 20:
            raise StorageError('连接符无效。')
        first = blocks[0]
        first.raw = copy.deepcopy(first.raw)
        first.raw['structure_edit_sources'] = [loc for block in blocks for loc in _loc(block)]
        first.source_text = separator.join(block.source_text for block in blocks)
        first.structure_revision += 1
        first.raw['structure_content_revision'] = first.structure_revision
        del document.blocks[positions[0] + 1:positions[-1] + 1]
    elif operation in {'edit_text', 'split'}:
        block = document.block(request.get('block_id') or '')
        _simple(block)
        if operation == 'edit_text':
            text = request.get('text')
            if not isinstance(text, str) or not text.strip():
                raise StorageError('文本不能为空。')
            if text == block.source_text:
                raise StorageError('文本没有变化。')
            block.source_text = text
            block.structure_revision += 1
            block.raw['structure_content_revision'] = block.structure_revision
        else:
            offset = request.get('offset')
            if isinstance(offset, bool) or not isinstance(offset, int) or not 0 < offset < len(block.source_text):
                raise StorageError('请选择块内有效的拆分位置。')
            if not block.source_text[:offset].strip() or not block.source_text[offset:].strip():
                raise StorageError('拆分后的文本不能为空。')
            second = copy.deepcopy(block)
            second.block_id = 'edit-' + uuid.uuid4().hex
            second.source_text = block.source_text[offset:]
            second.raw = copy.deepcopy(second.raw)
            second.raw['structure_edit_sources'] = _loc(block)
            second.raw['structure_edit_split_offset'] = offset
            second.structure_revision = 1
            second.raw['structure_content_revision'] = 1
            block.raw = copy.deepcopy(block.raw)
            block.raw['structure_edit_sources'] = _loc(block)
            block.source_text = block.source_text[:offset]
            block.structure_revision += 1
            block.raw['structure_content_revision'] = block.structure_revision
            document.blocks.insert(document.blocks.index(block) + 1, second)
    else:
        raise StorageError('不支持的结构编辑。')
    for index, block in enumerate(document.blocks):
        block.order = index
    document.toc = build_toc(document.blocks)


def _result(store: ProjectStore, project_id: str, document: Document, history: dict[str, Any]) -> dict[str, Any]:
    return {'document': document.to_dict(), 'can_undo': bool(history['undo']), 'can_redo': bool(history['redo'])}


def _check_active_jobs(store: ProjectStore, project_id: str) -> None:
    active_kinds = {'translate', 'refine', 'instant', 'review', 'reader_fold_run', 'reader_layers', 'structure_ai'}
    if any(job.get('kind') in active_kinds and job.get('status') in {'queued', 'running', 'pausing'} for job in store.list_job_summaries(project_id)):
        raise StorageError('文档正在翻译、结构校对或生成阅读内容，请等待任务结束或暂停后再编辑。')


def _check_removed_translations(store: ProjectStore, project_id: str, ids: set[str]) -> None:
    if not ids:
        return
    root = store.project_path(project_id) / 'translations'
    for path in root.glob('*/translation.json'):
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            entries = value.get('entries', {})
            if not isinstance(entries, dict):
                raise ValueError('invalid translation entries')
            if any(block_id in entries for block_id in ids):
                raise StorageError('所选块已有译文或翻译记录，暂不能合并或撤销拆分；请保留原块以避免译文失联。')
        except (OSError, ValueError, AttributeError) as error:
            raise StorageError('无法检查已有译文，已停止结构编辑。') from error


def _check_annotations(store: ProjectStore, project_id: str, ids: set[str]) -> None:
    if not ids:
        return
    overlay = ReaderStore(store).load_overlay(project_id)
    if any(item.get('block_id') in ids for item in overlay.get('annotations', [])):
        raise StorageError('所选内容包含阅读批注，暂不能编辑、合并或拆分；请先处理相关批注，避免丢失。')


def apply_edit(store: ProjectStore, project_id: str, request: dict[str, Any]) -> dict[str, Any]:
    with store.document_lock(project_id):
        _recover(store, project_id)
        _check_active_jobs(store, project_id)
        document = store.load_document(project_id)
        expected = request.get('expected_document_updated_at')
        if expected and expected != document.updated_at:
            raise StorageError('文档已被修改，请刷新后重试。')
        before = document.to_dict()
        ids = set(request.get('block_ids') or []) if request.get('operation') in {'merge', 'patch_batch'} else ({request.get('block_id')} if request.get('operation') in {'split', 'edit_text', 'patch'} else set())
        overlay_lock = _path_lock(ReaderStore(store).overlay_path(project_id))
        with overlay_lock:
            return _apply_checked(store, project_id, request, document, before)


def _apply_checked(store: ProjectStore, project_id: str, request: dict[str, Any], document: Document, before: dict[str, Any]) -> dict[str, Any]:
        try:
            _perform(document, request)
        except KeyError as error:
            raise StorageError('内容块不存在，请刷新后重试。') from error
        after = document.to_dict()
        before_blocks = {item['block_id']: item for item in before['blocks']}
        after_blocks = {item['block_id']: item for item in after['blocks']}
        affected = {key for key in before_blocks.keys() | after_blocks.keys() if before_blocks.get(key, {}).get('source_text') != after_blocks.get(key, {}).get('source_text')}
        _check_annotations(store, project_id, affected)
        for block_id in before_blocks.keys() & after_blocks.keys():
            if before_blocks[block_id]['source_text'] != after_blocks[block_id]['source_text']:
                block = document.block(block_id)
                block.raw['structure_content_revision'] = block.structure_revision
        after = document.to_dict()
        _check_removed_translations(store, project_id, before_blocks.keys() - after_blocks.keys())
        history = _history(store, project_id)
        history['undo'].append({'before': before, 'after': after, 'before_hash': _fingerprint(before), 'after_hash': _fingerprint(after)})
        history['undo'] = history['undo'][-30:]
        history['redo'] = []
        _commit(store, project_id, _fingerprint(before), document, history)
        return _result(store, project_id, document, history)


def move_history(store: ProjectStore, project_id: str, direction: str) -> dict[str, Any]:
    with store.document_lock(project_id):
        _recover(store, project_id)
        _check_active_jobs(store, project_id)
        history = _history(store, project_id)
        source, target = ('undo', 'redo') if direction == 'undo' else ('redo', 'undo')
        if not history[source]:
            raise StorageError('没有可撤销的操作。' if direction == 'undo' else '没有可重做的操作。')
        entry = history[source][-1]
        current = store.load_document(project_id)
        expected = entry['after_hash'] if direction == 'undo' else entry['before_hash']
        if _fingerprint(current.to_dict()) != expected:
            raise StorageError('文档在该操作后已被修改，无法安全撤销或重做，请刷新查看。')
        document = Document.from_dict(copy.deepcopy(entry['before' if direction == 'undo' else 'after']))
        current_blocks = {b.block_id: b.source_text for b in current.blocks}
        target_blocks = {b.block_id: b.source_text for b in document.blocks}
        changed_ids = {key for key in current_blocks.keys() | target_blocks.keys() if current_blocks.get(key) != target_blocks.get(key)}
        _check_removed_translations(store, project_id, current_blocks.keys() - target_blocks.keys())
        with _path_lock(ReaderStore(store).overlay_path(project_id)):
            _check_annotations(store, project_id, changed_ids)
            return _move_checked(store, project_id, history, source, target, entry, document)


def _move_checked(store: ProjectStore, project_id: str, history: dict[str, Any], source: str, target: str, entry: dict[str, Any], document: Document) -> dict[str, Any]:
        history[source].pop()
        history[target].append(entry)
        before_hash = entry['after_hash'] if source == 'undo' else entry['before_hash']
        _commit(store, project_id, before_hash, document, history)
        return _result(store, project_id, document, history)
