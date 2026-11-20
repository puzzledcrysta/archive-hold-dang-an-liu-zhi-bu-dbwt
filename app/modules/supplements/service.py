import json
from hashlib import sha256
from datetime import datetime, timezone
from app.errors import DataError
from app.settings import RULE_VERSION, UNIT
from app.store import connection
from app.modules.runs.repository import lookup
from app.modules.domain.rule import evaluate
from app.modules.reconcile.service import compare
from .reader import parse_supplement


def _now():
    return datetime.now(timezone.utc).isoformat()


def _latest_review(db, run_id):
    row = db.execute(
        'SELECT version, decision, changed FROM reviews WHERE run_id=? ORDER BY version DESC LIMIT 1',
        (run_id,)).fetchone()
    return dict(row) if row else None


def _load(db, run_id, supplement_id):
    row = db.execute('SELECT * FROM supplements WHERE run_id=? AND supplement_id=?',
                     (run_id, supplement_id)).fetchone()
    if row is None:
        raise DataError('SUPPLEMENT_UNKNOWN', '没有此留置补报', 404)
    return row


def _supplement_id(run_id, payload):
    digest = sha256(payload).hexdigest()
    return sha256(f'{RULE_VERSION}:{run_id}:{digest}'.encode()).hexdigest(), digest


def _recompute_accepted(accepted, holds_by_key):
    """在原批次可采信记录上并入补报留置，逐件重算处置日。"""
    rows = []
    for record in accepted:
        fields = dict(record['raw_fields'])
        extra = holds_by_key.get(record['key'])
        if extra is not None:
            fields['留置区段'] = extra if fields['留置区段'] == '无' else f"{fields['留置区段']};{extra}"
        amount, proof = evaluate(fields)
        rows.append({'key': record['key'], 'line': record['line'],
                     'revision': record['revision'], 'amount': format(amount, 'f'),
                     'unit': UNIT, 'raw': record['raw'],
                     'proof': [{'metric': label, 'value': str(value), 'unit': unit}
                               for label, value, unit in proof]})
    return rows


def _snapshot(entries, accepted, book, cumulative, current_seq):
    """基于截至本次补报的全部留置重算；baseline 始终对照原批次。"""
    holds_by_key = {key: ';'.join(parts) for key, parts in cumulative.items()}
    recalculated = _recompute_accepted(accepted, holds_by_key)
    by_key = {row['key']: row for row in recalculated}
    original = {row['key']: row for row in accepted}
    affected = []
    for entry in entries:
        key = entry['key']
        if key not in original:
            raise DataError('SUPPLEMENT_UNKNOWN_KEY',
                            f"第 {entry['line']} 行档案件 {key} 不在原批次可采信记录中")
        before, after = original[key], by_key[key]
        affected.append({'key': key, 'line': entry['line'], 'changed': before['amount'] != after['amount'],
                         'baseline_amount': before['amount'], 'current_amount': after['amount'],
                         'baseline_proof': before['proof'], 'current_proof': after['proof']})
    book_report = compare(recalculated, book)
    snapshot = {'seq': current_seq,
                'changed_keys': sorted(key for key, parts in cumulative.items()
                                       if original[key]['amount'] != by_key[key]['amount']),
                'affected': affected, 'accepted': recalculated, 'book': book_report}
    return snapshot, book_report


def _stored_supplements(db, run_id):
    return [dict(row) for row in db.execute(
        'SELECT supplement_id, seq, payload, payload_sha256, changed, frozen, cleared_version, created, snapshot '
        'FROM supplements WHERE run_id=? ORDER BY seq', (run_id,))]


def _effective(db, run_row):
    """按顺序叠加全部补报，得到当前生效的重算视图与冻结集合。"""
    run = json.loads(run_row['result'])
    accepted = [{**row, 'raw_fields': _raw_fields(run, row)} for row in run['accepted']]
    items = _stored_supplements(db, run_row['id'])
    held = {}
    # 每件档案的冻结数 = 覆盖它且仍未核清的补报数
    pending_by_key = {}
    for item in items:
        snapshot = json.loads(item['snapshot'])
        for entry in parse_supplement(item['payload']):
            held.setdefault(entry['key'], []).append(entry['holds'])
        if item['frozen'] == 1:
            for row in snapshot['affected']:
                if row['changed']:
                    pending_by_key[row['key']] = pending_by_key.get(row['key'], 0) + 1
    holds_by_key = {key: ';'.join(parts) for key, parts in held.items()}
    recalculated = _recompute_accepted(accepted, holds_by_key) if holds_by_key else run['accepted']
    book_report = compare(recalculated, run_row['book']) if holds_by_key else run['book']
    frozen_keys = sorted(key for key, count in pending_by_key.items() if count > 0)
    return {'run': run, 'items': items, 'recalculated': recalculated,
            'book': book_report, 'frozen_keys': frozen_keys}


def _raw_fields(run, row):
    """从原始来源行还原 evaluate 所需字段。"""
    header = run['source_header']
    return {name: value for name, value in zip(header, row['raw'])}


def submit(run_id, payload):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        run_row = lookup(db, run_id)
        review = _latest_review(db, run_id)
        if review is None or review['decision'] != '确认':
            raise DataError('SUPPLEMENT_NOT_CONFIRMED', '只有已确认的批次才能接收留置补报', 409)
        supplement_id, digest = _supplement_id(run_id, payload)
        existing = db.execute('SELECT * FROM supplements WHERE run_id=? AND supplement_id=?',
                              (run_id, supplement_id)).fetchone()
        if existing is not None:
            # 同一补报重跑：直接返回已存记录，不重复撤回、不重复记账
            return _detail_payload(dict(existing), duplicated=True)
        entries = parse_supplement(payload)
        run = json.loads(run_row['result'])
        accepted = [{**row, 'raw_fields': _raw_fields(run, row)} for row in run['accepted']]
        cumulative = {}
        for item in _stored_supplements(db, run_id):
            for past in parse_supplement(item['payload']):
                cumulative.setdefault(past['key'], []).append(past['holds'])
        for entry in entries:
            cumulative.setdefault(entry['key'], []).append(entry['holds'])
        seq = db.execute('SELECT COALESCE(MAX(seq),0)+1 FROM supplements WHERE run_id=?',
                         (run_id,)).fetchone()[0]
        snapshot, _ = _snapshot(entries, accepted, run_row['book'], cumulative, seq)
        created = _now()
        db.execute('INSERT INTO supplements VALUES (?,?,?,?,?,?,?,?,?,?)',
                   (run_id, supplement_id, seq, payload, digest,
                    len(snapshot['changed_keys']), 1, None, created,
                    json.dumps(snapshot, ensure_ascii=False)))
        for row in snapshot['affected']:
            db.execute('INSERT INTO supplement_keys VALUES (?,?,?,?,?,?)',
                       (run_id, supplement_id, row['key'], int(row['changed']),
                        row['baseline_amount'], row['current_amount']))
        return _detail_payload(_load(db, run_id, supplement_id), duplicated=False)


def _detail_payload(row, duplicated):
    snapshot = json.loads(row['snapshot'])
    return {'supplement_id': row['supplement_id'], 'run_id': row['run_id'], 'seq': row['seq'],
            'payload_sha256': row['payload_sha256'], 'created': row['created'],
            'frozen': bool(row['frozen']), 'cleared_version': row['cleared_version'],
            'duplicated': duplicated, 'affected': snapshot['affected'], 'book': snapshot['book']}


def list_supplements(run_id):
    with connection() as db:
        lookup(db, run_id)
        return [{'supplement_id': row['supplement_id'], 'seq': row['seq'],
                 'payload_sha256': row['payload_sha256'], 'created': row['created'],
                 'frozen': bool(row['frozen']), 'cleared_version': row['cleared_version'],
                 'changed': row['changed'],
                 'changed_keys': json.loads(row['snapshot'])['changed_keys']}
                for row in db.execute('SELECT * FROM supplements WHERE run_id=? ORDER BY seq', (run_id,))]


def detail(run_id, supplement_id):
    with connection() as db:
        return _detail_payload(_load(db, run_id, supplement_id), duplicated=False)


def effective(run_id):
    with connection() as db:
        state = _effective(db, lookup(db, run_id))
        review = _latest_review(db, run_id)
        run = state['run']
        return {'run_id': run_id,
                'confirmed': review is not None and review['decision'] == '确认',
                'latest_review': review,
                'frozen_keys': state['frozen_keys'],
                'supplements': [{'supplement_id': item['supplement_id'], 'seq': item['seq'],
                                 'frozen': bool(item['frozen']),
                                 'cleared_version': item['cleared_version'],
                                 'created': item['created']} for item in state['items']],
                'counts': run['counts'],
                'accepted': state['recalculated'],
                'original_book': run['book'], 'book': state['book']}


def release_state(run_id):
    """放行闸口：差异核清前，受影响档案不能再凭旧确认放行。"""
    with connection() as db:
        state = _effective(db, lookup(db, run_id))
        review = _latest_review(db, run_id)
        confirmed = review is not None and review['decision'] == '确认'
        rows = []
        for row in state['recalculated']:
            frozen = row['key'] in state['frozen_keys']
            rows.append({'key': row['key'], 'amount': row['amount'], 'frozen': frozen,
                         'releasable': confirmed and not frozen,
                         'reason': 'PENDING_RECONCILE' if frozen else
                         ('NOT_CONFIRMED' if not confirmed else 'OK')})
        return {'run_id': run_id, 'confirmed': confirmed, 'frozen_keys': state['frozen_keys'],
                'all_releasable': confirmed and not state['frozen_keys'], 'rows': rows}


def mark_cleared(db, run_id, review_version):
    """新的「确认」复核登记后调用：把仍在冻结的补报标记为已核清并解除冻结。"""
    db.execute('UPDATE supplements SET frozen=0, cleared_version=? WHERE run_id=? AND frozen=1',
               (review_version, run_id))
    return db.execute('SELECT changes()').fetchone()[0]


def reopen_pending(db, run_id):
    """新的「退回」复核登记后调用：已核清的补报重新回到冻结，等待再次核清。"""
    db.execute('UPDATE supplements SET frozen=1, cleared_version=NULL WHERE run_id=? AND frozen=0',
               (run_id,))
    return db.execute('SELECT changes()').fetchone()[0]
