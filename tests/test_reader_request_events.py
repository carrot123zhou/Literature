from copy import copy
import pytest
from literature.providers import ProviderError, ProviderResponse
from literature.reader import ReaderLayerService
from literature.reader_batches import ActivityLog
from literature.storage import ProjectStore


class StreamingFake:
    def __init__(self, mode='stream'):
        self.mode = mode
        self.closed = []
        self.on_activity = None
        self.settled = []
        self._fold_settle = lambda provider, response, error: self.settled.append((response, error))

    def with_request_limits(self, **kwargs):
        if self.mode == 'clone-error':
            raise ProviderError('clone construction failed', possible_charge=False)
        child = copy(self)
        child.on_activity = kwargs['on_activity']
        return child

    def complete(self, **kwargs):
        self.on_activity({'stage': 'waiting', 'request_started_at': '2026-10-27T01:00:00Z'})
        self.on_activity({'stage': 'generating', 'last_response_at': '2026-10-27T01:00:01Z'})
        response = ProviderResponse(content='{"units":[],"section":{"takeaway":"A complete response"}}',
                                    provider='fake', model='frozen', input_tokens=100, output_tokens=50,
                                    finish_reason='incomplete_stream' if self.mode == 'incomplete' else 'stop')
        if self.mode == 'known-error':
            raise ProviderError('output exceeded', response=response, possible_charge=True)
        return response

    def close(self):
        self.closed.append(True)


def call(tmp_path, provider):
    service = ReaderLayerService(ProjectStore(tmp_path))
    events, usage = [], []
    outcome = service._call_model(provider, provider_factory=None, system='s', user='u',
                                  section_id='S1', stage='derive', batch_label='b1', batch_index=0,
                                  batch_total=1, attempt=1, units=['a'], activity=ActivityLog(events.append),
                                  usage_hook=usage.append)
    return outcome, events, usage


def test_real_stream_metadata_reaches_activity_without_duplicate_stage(tmp_path):
    provider = StreamingFake()
    outcome, events, usage = call(tmp_path, provider)
    assert outcome.payload is not None, outcome.error
    assert any(event.get('stage') == 'generating' for event in events)
    assert any(event.get('request_started_at') for event in events)
    assert len(provider.closed) == 1 and len(usage) == 1


@pytest.mark.parametrize('mode', ['known-error', 'incomplete'])
def test_known_usage_is_recorded_without_applying_failed_payload(tmp_path, mode):
    provider = StreamingFake(mode)
    outcome, _, usage = call(tmp_path, provider)
    assert outcome.payload is None and outcome.error
    assert len(usage) == 1 and usage[0].output_tokens == 50
    assert provider.settled[0][0] is usage[0]
    assert len(provider.closed) == 1


def test_clone_failure_never_falls_back_to_unlimited_provider(tmp_path):
    outcome, _, usage = call(tmp_path, StreamingFake('clone-error'))
    assert outcome.payload is None and 'clone construction failed' in outcome.error
    assert not usage
