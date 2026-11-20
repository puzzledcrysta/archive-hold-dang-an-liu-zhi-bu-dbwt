import pytest
from app import settings


@pytest.fixture
def confirmed_run(client):
    run_id = client.post('/api/runs', json={}).json()['manifest']['run_id']
    client.post(f'/api/review/{run_id}',
                json={'version': 0, 'decision': '确认', 'note': '批次首次确认'})
    return run_id


HOLD_COVERING = '档案件,留置区段\n00101,2025-02-20/2025-03-10\n'
HOLD_OTHER_KEY = '档案件,留置区段\n00104,2026-01-01/2026-02-01\n'


def test_only_confirmed_run_accepts_supplement(client):
    run_id = client.post('/api/runs', json={}).json()['manifest']['run_id']
    refused = client.post(f'/api/runs/{run_id}/supplements', json={'data': HOLD_COVERING})
    assert (refused.status_code, refused.json()['code']) == (409, 'SUPPLEMENT_NOT_CONFIRMED')
    assert client.get(f'/api/runs/{run_id}/supplements').json()['supplements'] == []


def test_supplement_recalculates_disposal_and_reconciles(client, confirmed_run):
    response = client.post(f'/api/runs/{confirmed_run}/supplements', json={'data': HOLD_COVERING})
    assert response.status_code == 200
    payload = response.json()
    affected, = payload['affected']
    # 00101 原处置日 2025-03-01 被留置推到 2025-03-10，366 天 -> 375 天
    assert (affected['key'], affected['baseline_amount'], affected['current_amount'], affected['changed']) \
        == ('00101', '366', '375', True)
    # 重新对账：原批次 3 条差异，重算后 00101 也产生差值
    assert payload['book']['mismatched'] == 4
    effective = client.get(f'/api/runs/{confirmed_run}/effective').json()
    row = next(item for item in effective['accepted'] if item['key'] == '00101')
    assert row['amount'] == '375'
    assert effective['original_book']['mismatched'] == 3 and effective['book']['mismatched'] == 4


def test_affected_archive_frozen_until_cleared(client, confirmed_run):
    client.post(f'/api/runs/{confirmed_run}/supplements', json={'data': HOLD_COVERING})
    gate = client.get(f'/api/runs/{confirmed_run}/release').json()
    assert gate['all_releasable'] is False and gate['frozen_keys'] == ['00101']
    rows = {row['key']: row for row in gate['rows']}
    assert rows['00101']['releasable'] is False and rows['00101']['reason'] == 'PENDING_RECONCILE'
    # 同批其他档案不撤回、照常放行
    assert rows['00102']['releasable'] is True and rows['00102']['reason'] == 'OK'


def test_rerun_same_supplement_is_idempotent(client, confirmed_run):
    first = client.post(f'/api/runs/{confirmed_run}/supplements', json={'data': HOLD_COVERING}).json()
    second = client.post(f'/api/runs/{confirmed_run}/supplements', json={'data': HOLD_COVERING}).json()
    assert first['supplement_id'] == second['supplement_id'] and second['duplicated'] is True
    listing = client.get(f'/api/runs/{confirmed_run}/supplements').json()['supplements']
    assert len(listing) == 1 and listing[0]['changed_keys'] == ['00101']
    # 重跑不产生新的冻结记录
    assert client.get(f'/api/runs/{confirmed_run}/release').json()['frozen_keys'] == ['00101']


def test_reconfirm_clears_and_releases(client, confirmed_run):
    client.post(f'/api/runs/{confirmed_run}/supplements', json={'data': HOLD_COVERING})
    # 有未核清补报时必须填写意见
    missing_note = client.post(f'/api/review/{confirmed_run}',
                               json={'version': 1, 'decision': '确认', 'note': ''})
    assert (missing_note.status_code, missing_note.json()['code']) == (422, 'REVIEW_NOTE')
    cleared = client.post(f'/api/review/{confirmed_run}',
                          json={'version': 1, 'decision': '确认', 'note': '差异已核清，重新确认'})
    assert cleared.status_code == 200 and cleared.json()['version'] == 2
    gate = client.get(f'/api/runs/{confirmed_run}/release').json()
    assert gate['all_releasable'] is True and gate['frozen_keys'] == []
    record = client.get(f'/api/runs/{confirmed_run}/supplements').json()['supplements'][0]
    assert record['frozen'] is False and record['cleared_version'] == 2


def test_return_decision_reopens_freeze(client, confirmed_run):
    client.post(f'/api/runs/{confirmed_run}/supplements', json={'data': HOLD_COVERING})
    client.post(f'/api/review/{confirmed_run}',
                json={'version': 1, 'decision': '确认', 'note': '核清'})
    client.post(f'/api/review/{confirmed_run}',
                json={'version': 2, 'decision': '退回', 'note': '差异仍需复查'})
    gate = client.get(f'/api/runs/{confirmed_run}/release').json()
    assert gate['confirmed'] is False and gate['frozen_keys'] == ['00101']
    record = client.get(f'/api/runs/{confirmed_run}/supplements').json()['supplements'][0]
    assert record['frozen'] is True and record['cleared_version'] is None


def test_original_confirmation_and_evidence_remain_queryable(client, confirmed_run):
    original_run = client.get(f'/api/runs/{confirmed_run}').json()
    client.post(f'/api/runs/{confirmed_run}/supplements', json={'data': HOLD_COVERING})
    client.post(f'/api/review/{confirmed_run}',
                json={'version': 1, 'decision': '确认', 'note': '核清后重新确认'})
    # 原批次结果不被改写
    stored_run = client.get(f'/api/runs/{confirmed_run}').json()
    assert stored_run == original_run
    row = next(item for item in stored_run['accepted'] if item['key'] == '00101')
    assert row['amount'] == '366' and row['proof']
    # 两次确认的复核记录都在
    timeline = client.get(f'/api/review/{confirmed_run}').json()
    assert [item['version'] for item in timeline] == [2, 1]
    assert timeline[1]['note'] == '批次首次确认'
    # 导出证据同时包含原依据、补报与放行状态
    bundle = client.get(f'/api/export/{confirmed_run}').json()
    assert bundle['supplements'] and bundle['release']['all_releasable'] is True
    assert any(item['key'] == '00101' for item in bundle['results'])


def test_no_change_supplement_does_not_freeze(client, confirmed_run):
    # 00104 的最早处置日为 2027-01-01，2026 年的留置不覆盖处置日，计量不变
    payload = client.post(f'/api/runs/{confirmed_run}/supplements',
                          json={'data': HOLD_OTHER_KEY}).json()
    assert payload['affected'][0]['changed'] is False
    assert client.get(f'/api/runs/{confirmed_run}/release').json()['frozen_keys'] == []


def test_multiple_supplements_accumulate_holds(client, confirmed_run):
    first = client.post(f'/api/runs/{confirmed_run}/supplements',
                        json={'data': HOLD_COVERING}).json()
    second = client.post(f'/api/runs/{confirmed_run}/supplements',
                         json={'data': '档案件,留置区段\n00101,2025-03-10/2025-03-15\n'}).json()
    assert first['seq'] == 1 and second['seq'] == 2
    effective = client.get(f'/api/runs/{confirmed_run}/effective').json()
    row = next(item for item in effective['accepted'] if item['key'] == '00101')
    # 连续留置继续顺延到 2025-03-15：366 + 14 = 380
    assert row['amount'] == '380'
    assert client.get(f'/api/runs/{confirmed_run}/release').json()['frozen_keys'] == ['00101']
    client.post(f'/api/review/{confirmed_run}',
                json={'version': 1, 'decision': '确认', 'note': '一并核清'})
    assert client.get(f'/api/runs/{confirmed_run}/release').json()['frozen_keys'] == []


def test_invalid_supplements_rejected_without_side_effects(client, confirmed_run):
    cases = [
        ('x,y\n1,2\n', 'SUPPLEMENT_HEADER'),
        ('档案件,留置区段\n\n', 'SUPPLEMENT_EMPTY'),
        ('档案件,留置区段\n00101\n', 'SUPPLEMENT_WIDTH'),
        ('档案件,留置区段\n00101,2025-03-10/2025-02-20\n', 'ARCHIVE_HOLD'),
        ('档案件,留置区段\n99999,2025-02-20/2025-03-10\n', 'SUPPLEMENT_UNKNOWN_KEY'),
        ('档案件,留置区段\n00101,2025-01-01/2025-01-05\n00101,2025-02-01/2025-02-05\n', 'SUPPLEMENT_WIDTH'),
    ]
    for data, code in cases:
        response = client.post(f'/api/runs/{confirmed_run}/supplements', json={'data': data})
        assert (response.status_code, response.json()['code']) == (422, code)
    assert client.get(f'/api/runs/{confirmed_run}/supplements').json()['supplements'] == []
    assert client.get(f'/api/runs/{confirmed_run}/release').json()['all_releasable'] is True


def test_supplement_detail_and_listing(client, confirmed_run):
    payload = client.post(f'/api/runs/{confirmed_run}/supplements',
                          json={'data': HOLD_COVERING}).json()
    listing = client.get(f'/api/runs/{confirmed_run}/supplements').json()['supplements']
    assert len(listing) == 1 and listing[0]['supplement_id'] == payload['supplement_id']
    detail = client.get(f'/api/runs/{confirmed_run}/supplements/{payload["supplement_id"]}').json()
    assert detail['affected'] == payload['affected'] and detail['book']['mismatched'] == 4
    missing = client.get(f'/api/runs/{confirmed_run}/supplements/not-there')
    assert missing.status_code == 404


def test_encoded_supplement_matches_text(client, confirmed_run):
    from base64 import b64encode
    encoded = b64encode(HOLD_COVERING.encode()).decode()
    via_text = client.post(f'/api/runs/{confirmed_run}/supplements',
                           json={'data': HOLD_COVERING}).json()
    via_base64 = client.post(f'/api/runs/{confirmed_run}/supplements',
                             json={'data_base64': encoded}).json()
    assert via_text['supplement_id'] == via_base64['supplement_id']
    assert via_base64['duplicated'] is True


def test_returned_run_closes_supplement_channel_until_reconfirm(client, confirmed_run):
    client.post(f'/api/runs/{confirmed_run}/supplements', json={'data': HOLD_COVERING})
    client.post(f'/api/review/{confirmed_run}',
                json={'version': 1, 'decision': '退回', 'note': '退回复查'})
    refused = client.post(f'/api/runs/{confirmed_run}/supplements',
                          json={'data': HOLD_OTHER_KEY})
    assert (refused.status_code, refused.json()['code']) == (409, 'SUPPLEMENT_NOT_CONFIRMED')
    # 重新确认后通道恢复，在途补报同步解冻
    client.post(f'/api/review/{confirmed_run}',
                json={'version': 2, 'decision': '确认', 'note': '复查后重新确认'})
    gate = client.get(f'/api/runs/{confirmed_run}/release').json()
    assert gate['all_releasable'] is True
    accepted = client.post(f'/api/runs/{confirmed_run}/supplements',
                           json={'data': HOLD_OTHER_KEY})
    assert accepted.status_code == 200 and accepted.json()['seq'] == 2
