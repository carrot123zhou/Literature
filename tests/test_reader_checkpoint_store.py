from pathlib import Path
from literature.reader_batches import UnitCheckpoints


def item(uid):
    return {'id': uid, 'source': 'model', 'takeaway': 'A valid short layer.'}


def test_independent_writers_merge_units_summaries_and_lanes(tmp_path):
    path = tmp_path / 'fold_checkpoints.json'
    a, b = UnitCheckpoints(path), UnitCheckpoints(path)
    a.put_unit('source', 'S1', 'a', 'fp-a', item('a'))
    b.put_unit('source', 'S1', 'b', 'fp-b', item('b'))
    b.put_summary('zh-CN', 'S1', fingerprint='summary', takeaway='主线', topic='主线')
    a.save()
    b.save()
    check = UnitCheckpoints(path)
    assert check.unit('source', 'S1', 'a', 'fp-a') == item('a')
    assert check.unit('source', 'S1', 'b', 'fp-b') == item('b')
    assert check.summary('zh-CN', 'S1', 'summary')['takeaway'] == '主线'


def test_reader_notices_other_writer_atomic_update(tmp_path):
    path = tmp_path / 'fold_checkpoints.json'
    a, b = UnitCheckpoints(path), UnitCheckpoints(path)
    a.put_unit('source', 'S1', 'a', 'one', item('a'))
    a.save()
    assert b.unit('source', 'S1', 'a', 'one')
    a.put_unit('source', 'S1', 'a', 'two', item('a'))
    a.save()
    assert b.unit('source', 'S1', 'a', 'one') is None
    assert b.unit('source', 'S1', 'a', 'two')


def test_unit_lookups_parse_unchanged_file_only_once(tmp_path, monkeypatch):
    path = tmp_path / 'fold_checkpoints.json'
    writer = UnitCheckpoints(path)
    for index in range(100):
        writer.put_unit('source', 'S1', str(index), str(index), item(str(index)))
    writer.save()
    reader = UnitCheckpoints(path)
    reads = []
    original = Path.read_text
    def counted(self, *args, **kwargs):
        if self == path:
            reads.append(True)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', counted)
    for index in range(100):
        assert reader.unit('source', 'S1', str(index), str(index))
    assert len(reads) == 1
