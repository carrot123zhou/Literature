"""Implicit start on a paused run must use its frozen binding, too."""
from fastapi.testclient import TestClient
from literature.api import create_app
from literature.reader import ReaderStore, new_fold_run
from literature.providers import MockProvider
from literature.storage import ProjectStore
from tests.test_reader_fold_run import make_document


def test_start_resumes_frozen_service_without_current_role(tmp_path, monkeypatch):
    source = tmp_path / 'book.pdf'
    source.write_bytes(b'%PDF-1.4\n% fixture')
    store = ProjectStore(tmp_path / 'library')
    pid = store.create_project(source, title='Frozen')['project_id']
    store.save_document(make_document(pid))
    app = create_app(store)
    manager = app.state.job_manager
    run = new_fold_run(run_id='fr-frozen', locale='source', total_sections=2,
                       binding={'service_id': 'original-service', 'model': 'original-model'})
    run['status'] = 'paused'
    ReaderStore(store).save_fold_run(pid, run)
    routes = []

    def provider_from(service_id, provider_input=None, model=''):
        routes.append((service_id, model))
        return MockProvider()

    def start(project_id, **kwargs):
        kwargs['provider_factory']()
        return run, True

    monkeypatch.setattr(manager, 'provider_from', provider_from)
    monkeypatch.setattr(manager.reader_jobs, 'start_run', start)
    with TestClient(app, headers={'x-literature-token': app.state.session_token}) as client:
        result = client.post(f'/projects/{pid}/reader/fold-runs', json={'locale': 'source'})
    assert result.status_code == 200, result.text
    assert routes == [('original-service', 'original-model')]
