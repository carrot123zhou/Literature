"""Regressions for the real 81-section run's overflowing resume counts."""
from literature.reader import ReaderJobRunner, ReaderStore, new_fold_run
from literature.reader_progress import section_progress
from literature.storage import ProjectStore


def make_run(tmp_path):
    store = ProjectStore(tmp_path / "library")
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-1.4\n% fixture")
    project_id = store.create_project(source, title="Progress")['project_id']
    job_id = store.create_job(project_id, "reader_fold_run", {}, {})
    run = new_fold_run(run_id="fr-progress", locale="source", binding={}, total_sections=81)
    states = {f"S{i}": "reused" if i < 68 else "pending" for i in range(81)}
    run.update(job_id=job_id, section_states=states,
               progress={**section_progress(states), "current_section": ""})
    reader = ReaderStore(store)
    reader.save_fold_run(project_id, run)
    return store, reader, ReaderJobRunner(store), project_id, job_id


def test_repeated_callbacks_never_exceed_total(tmp_path):
    store, reader, runner, pid, job_id = make_run(tmp_path)
    callback = runner._run_on_section(pid, "fr-progress", 81, job_id)
    for _ in range(3):
        for i in range(68):
            callback(f"S{i}", "reused")
    callback("S68", "folded")
    callback("S68", "folded")
    callback("S69", "failed")
    progress = reader.load_fold_run(pid)["progress"]
    assert progress == dict(total=81, completed=1, reused=68, failed=1, pending=12,
                            ratio=round(69 / 81, 3), current_section="S69")
    assert store.get_job(pid, job_id)["progress"] == round(69 / 81, 3)


def test_request_position_does_not_overwrite_ready_progress(tmp_path):
    _, reader, runner, pid, job_id = make_run(tmp_path)
    runner._run_progress_for(pid, "fr-progress", job_id)(0.2, "准备 S49")
    run = reader.load_fold_run(pid)
    assert run['progress']['ratio'] == round(68 / 81, 3)
    assert run['progress']['current_section'] == 'S49'


def test_late_attempt_cannot_overwrite_same_run_resume(tmp_path):
    _, reader, runner, pid, job_id = make_run(tmp_path)
    old_progress = runner._run_progress_for(pid, 'fr-progress', 'old-job')
    old_section = runner._run_on_section(pid, 'fr-progress', 81, 'old-job')
    old_activity = runner._run_activity_for(pid, 'fr-progress', 'old-job')
    before = reader.load_fold_run(pid)
    old_progress(0.99, '旧响应 S80')
    old_section('S80', 'folded')
    old_activity({'stage': 'generating', 'pending_units': 100})
    assert reader.load_fold_run(pid) == before


def test_activity_is_metadata_only_and_does_not_regress_pausing(tmp_path):
    _, reader, runner, pid, job_id = make_run(tmp_path)
    run = reader.load_fold_run(pid)
    run['status'] = 'pausing'
    reader.save_fold_run(pid, run)
    runner._run_activity_for(pid, 'fr-progress', job_id)({
        'stage': 'generating', 'pending_units': 4, 'content': 'unvalidated model text',
    })
    after = reader.load_fold_run(pid)
    assert after['status'] == 'pausing'
    assert after['activity'] == {'stage': 'generating', 'pending_units': 4}
