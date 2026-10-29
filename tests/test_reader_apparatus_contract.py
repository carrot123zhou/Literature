import pytest
from literature.reader_apparatus import candidate_kind, classifier_prompt, parse_classifier_reply, apparatus_fingerprint
from literature.reader_batches import estimate_tokens
from third_party.textfold import tokens


@pytest.mark.parametrize('title,kind', [
    ('Index', 'index'), ('Primary Sources', 'bibliography'), ('Secondary Sources', 'bibliography'),
    ('Chapter VII References', 'bibliography'), ('七、参考文献', 'bibliography'),
    ('Harvard-Yenching Institute Monograph Series (title', 'catalog'),
    ('Sources of Ritual Thought', None), ('Indexing Religious Practices', None),
])
def test_explicit_candidates_do_not_consume_roman_word_prefix(title, kind):
    assert candidate_kind(title) == kind


def test_ai_verdict_must_be_explicit_and_unmixed():
    assert not parse_classifier_reply({'kind': 'index', 'mixed': True})['affirmative']
    assert not parse_classifier_reply({'kind': 'index', 'mixed': 'false'})['ok']
    assert not parse_classifier_reply({'kind': 'body', 'mixed': False})['affirmative']
    assert parse_classifier_reply({'kind': 'index', 'mixed': False})['affirmative']


def test_classifier_large_cjk_keeps_first_middle_last_samples_within_budget():
    units = {f'u{i}': {'toks': tokens.tokenize_text('参考文献作者出版社卷次' * 500), 'type': 'paragraph'} for i in range(40)}
    chunk = {'id': 'S1', 'title': '参考文献', 'units': list(units)}
    doc = {'units': units, 'atoms': {}, 'meta': {'title': '书目', 'lang': 'zh'}}
    system, user = classifier_prompt(doc, chunk)
    assert estimate_tokens(system) + estimate_tokens(user) <= 6000
    assert '[u0]' in user and '[u20]' in user and '[u39]' in user


def test_receipt_is_bound_to_model_service_and_source():
    base = dict(section_digest='a', service_id='s', model='m')
    first = apparatus_fingerprint(**base)
    for key in base:
        assert apparatus_fingerprint(**{**base, key: 'different'}) != first
