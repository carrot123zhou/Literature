"""Regression for real BEYOND cache restoration adding a stop on every resume."""
from literature.reader import _restore_source_lads, _restore_target_lads
from literature.textfold_adapter import tf_tokens


def test_source_restoration_preserves_stored_key_and_takeaway():
    item = {'id': 'a', 'source': 'model', 'key': 'A temple inspires reverence. Rituals sanctify space.',
            'takeaway': 'A temple inspires reverence', 'topic': 'temple', 'evidence': [0]}
    doc = {'units': {'a': {}}, 'atoms': {}, 'is_cjk': False}
    chunk = {'units': ['a']}
    for _ in range(4):
        _restore_source_lads(doc, chunk, {'units': [item]})
        lad = doc['units']['a']['lad']
        assert tf_tokens.plain(lad['brief'], {}) == item['key']
        assert tf_tokens.plain(lad['brief'][:lad['tn']], {}) == item['takeaway']


def test_target_restoration_does_not_append_repeated_cjk_stops():
    item = {'id': 'a', 'source': 'model', 'key': '唐廷在洞府行仪式。仪式确立合法性。',
            'takeaway': '唐廷在洞府行仪式', 'topic': '仪式', 'evidence': [0],
            'brief': ['原文证据的分层译句。'], 'brief_source': 'derived'}
    doc = {'units': {'a': {'tr': {'zh-CN': {'toks': [{'t': '译'}]}}}}, 'atoms': {}}
    chunk = {'units': ['a']}
    for _ in range(4):
        _restore_target_lads(doc, chunk, locale='zh-CN', stored_section={'units': [item]})
        lad = doc['units']['a']['tr']['zh-CN']['lad']
        assert tf_tokens.plain(lad['brief'], {}) == item['key']
        assert tf_tokens.plain(lad['brief'][:lad['tn']], {}) == item['takeaway']
        assert lad['brief_sentences'] == item['brief']


def test_classified_backmatter_resume_never_resurrects_old_short_layers(tmp_path):
    from tests.test_reader_batches import FakeProvider, make_document, paragraph
    from literature.storage import ProjectStore
    from literature.reader import ReaderLayerService
    from literature.models import TranslationEntry, TranslationLayer
    store = ProjectStore(tmp_path / 'library')
    origin = tmp_path / 'book.pdf'
    origin.write_bytes(b'%PDF-1.4')
    pid = store.create_project(origin, title='References')['project_id']
    doc = make_document(pid, sections={'References': [paragraph(12), paragraph(13)]})
    store.save_document(doc)
    store.save_translation(TranslationLayer(project_id=pid, locale='zh-CN', entries={
        block.block_id: TranslationEntry(block_id=block.block_id, text='完整译文保留。')
        for block in doc.blocks
    }))
    service = ReaderLayerService(store)
    # Old source checkpoints existed before the Full-only back-matter policy.
    service.generate(pid, provider=FakeProvider(), service_id='svc', model_label='m')
    cp = service.checkpoints(pid)
    payload = __import__('json').loads(cp.path.read_text())
    payload['sections']['source']['S1'].pop('apparatus', None)
    cp.path.write_text(__import__('json').dumps(payload))
    classifier = FakeProvider(classify={'kind': 'bibliography', 'mixed': False, 'reason': 'list'})
    service.generate(pid, locale='zh-CN', provider=classifier, service_id='svc', model_label='m')
    assert classifier.calls == ['classify']
    for _ in range(3):
        cached = FakeProvider()
        result = ReaderLayerService(store).generate(pid, locale='zh-CN', provider=cached,
                                                   service_id='svc', model_label='m')
        assert result['failed'] == []
        assert cached.calls == []
        assert service.fold_run_readiness(pid, locale='zh-CN', service_id='svc', model='m')['ready']


def test_legacy_partial_target_keeps_valid_units_when_one_source_unit_is_repaired():
    from tests.test_reader_batches import LegacySeedTests, FakeProvider
    from literature.reader import ReaderStore
    fixture = LegacySeedTests()
    fixture.setUp()
    try:
        fixture.service.generate(fixture.project_id, locale='zh-CN', provider=FakeProvider(),
                                 service_id='svc', model_label='m')
        fixture._make_legacy_state()
        reader = ReaderStore(fixture.store)
        layers = reader.load_layers(fixture.project_id)
        target = layers['docs']['zh-CN']['sections'][0]
        retained = dict(target['units'][0])
        target['units'][1]['source'] = 'pending'
        import json
        checkpoint_path = fixture.service.checkpoints(fixture.project_id).path
        checkpoint_data = json.loads(checkpoint_path.read_text())
        checkpoint_data['sections'].pop('zh-CN', None)
        checkpoint_path.write_text(json.dumps(checkpoint_data))
        reader.save_locale_doc(fixture.project_id, 'zh-CN', layers['docs']['zh-CN'],
                               layers['section_hashes']['zh-CN'])
        provider = FakeProvider()
        result = fixture.service.generate(fixture.project_id, locale='zh-CN', provider=provider,
                                           service_id='svc', model_label='m')
        assert result['failed'] == []
        derive_prompts = [text for kind, text in provider.prompts if kind == 'derive']
        assert len(derive_prompts) == 1
        assert FakeProvider._unit_ids(derive_prompts[0]) == ['U0']
        after = reader.load_layers(fixture.project_id)['docs']['zh-CN']['sections'][0]['units'][0]
        assert all(after.get(k) == retained.get(k) for k in ['key', 'takeaway', 'topic', 'evidence', 'brief'])
    finally:
        fixture.tearDown()


def test_source_summary_failure_resume_only_retries_summary(tmp_path):
    from tests.test_reader_batches import FakeProvider, make_document, paragraph
    from literature.storage import ProjectStore
    from literature.reader import ReaderLayerService
    store = ProjectStore(tmp_path / 'library')
    origin = tmp_path / 'book.pdf'
    origin.write_bytes(b'%PDF-1.4')
    pid = store.create_project(origin, title='Summary')['project_id']
    store.save_document(make_document(pid, sections={'Body': [paragraph(12, i) for i in range(25)]}))
    service = ReaderLayerService(store)
    failed = service.generate(pid, provider=FakeProvider(fail_summary=True), service_id='svc', model_label='m')
    assert failed['failed']
    resumed = FakeProvider()
    result = service.generate(pid, provider=resumed, service_id='svc', model_label='m')
    assert result['failed'] == []
    assert resumed.calls == ['summary']
    assert service.fold_run_readiness(pid, locale='source', service_id='svc', model='m')['ready']


def test_pause_before_first_section_preserves_all_existing_projections(tmp_path):
    import threading
    from tests.test_reader_batches import FakeProvider, make_document, paragraph
    from literature.storage import ProjectStore
    from literature.reader import ReaderLayerService, ReaderStore
    store = ProjectStore(tmp_path / 'library')
    origin = tmp_path / 'book.pdf'
    origin.write_bytes(b'%PDF-1.4')
    pid = store.create_project(origin, title='Pause')['project_id']
    store.save_document(make_document(pid, sections={'First': [paragraph(12)], 'Last': [paragraph(13)]}))
    service = ReaderLayerService(store)
    service.generate(pid, provider=FakeProvider(), service_id='svc', model_label='m')
    reader = ReaderStore(store)
    before = reader.load_layers(pid)['docs']['source']['sections']
    pause = threading.Event()
    pause.set()
    result = service.generate(pid, provider=FakeProvider(), service_id='svc', model_label='m', pause=pause)
    assert result['paused']
    assert reader.load_layers(pid)['docs']['source']['sections'] == before


def test_legacy_pending_source_never_overwrites_a_valid_target_checkpoint():
    from tests.test_reader_batches import LegacySeedTests, FakeProvider
    fixture = LegacySeedTests()
    fixture.setUp()
    try:
        fixture.service.generate(fixture.project_id, locale='zh-CN', provider=FakeProvider(),
                                 service_id='svc', model_label='m')
        fixture._make_legacy_state()
        provider = FakeProvider()
        result = fixture.service.generate(fixture.project_id, locale='zh-CN', provider=provider,
                                           service_id='svc', model_label='m')
        assert result['failed'] == []
        assert provider.calls.count('fold') == 1
        assert provider.calls.count('derive') == 0
    finally:
        fixture.tearDown()
