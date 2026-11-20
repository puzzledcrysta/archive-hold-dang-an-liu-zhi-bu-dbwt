from app.errors import DataError
from app.settings import KEY
from app.modules.intake.reader import decode, records

SUPPLEMENT_FIELDS = (KEY, '留置区段')


def parse_supplement(payload):
    """解析留置补报：每行给出档案件与新增的留置区段（可多项，分号隔开）。"""
    text, _ = decode(payload)
    if not text.strip():
        raise DataError('SUPPLEMENT_EMPTY', '留置补报没有有效记录')
    rows = iter(records(text))
    _, header = next(rows, (0, []))
    if header != list(SUPPLEMENT_FIELDS):
        raise DataError('SUPPLEMENT_HEADER', f'补报表头须为 {SUPPLEMENT_FIELDS[0]}、留置区段')
    entries = []
    seen = set()
    for line, row in rows:
        if not row or not any(value.strip() for value in row):
            continue
        if len(row) != 2:
            raise DataError('SUPPLEMENT_WIDTH', f'补报第 {line} 行列数应为 2')
        key, hold = row[0].strip(), row[1].strip()
        if not key or not hold:
            raise DataError('SUPPLEMENT_WIDTH', f'补报第 {line} 行缺少档案件或留置区段')
        if key in seen:
            raise DataError('SUPPLEMENT_WIDTH', f'补报第 {line} 行档案件 {key} 重复')
        seen.add(key)
        entries.append({'line': line, 'key': key, 'holds': hold})
    if not entries:
        raise DataError('SUPPLEMENT_EMPTY', '留置补报没有有效记录')
    return entries
