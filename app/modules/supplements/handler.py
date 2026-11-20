from fastapi import APIRouter
from pydantic import BaseModel, Field
from app.errors import DataError
from .service import submit, list_supplements, detail, effective, release_state

router = APIRouter()
MAX_BYTES = 128 * 1024


class SupplementIn(BaseModel):
    data: str | None = Field(default=None, max_length=MAX_BYTES)
    data_base64: str | None = Field(default=None, max_length=(MAX_BYTES + 2) // 3 * 4)

    def payload(self):
        if self.data is None and self.data_base64 is None:
            raise DataError('SUPPLEMENT_EMPTY', '请提交留置补报 CSV')
        if self.data is not None and self.data_base64 is not None:
            raise DataError('CONTRACT', '同一输入只能提交文本或原始文件')
        if self.data_base64 is not None:
            from base64 import b64decode
            from binascii import Error as Base64Error
            try:
                raw = b64decode(self.data_base64, validate=True)
            except (ValueError, Base64Error) as error:
                raise DataError('ENCODING', '原始文件编码无效') from error
        else:
            raw = self.data.encode()
        if len(raw) > MAX_BYTES:
            raise DataError('LIMIT', '补报文件不能超过 128 KiB', 413)
        return raw


@router.post('/api/runs/{run_id}/supplements')
def add_supplement(run_id: str, body: SupplementIn):
    return submit(run_id, body.payload())


@router.get('/api/runs/{run_id}/supplements')
def supplements(run_id: str):
    return {'run_id': run_id, 'supplements': list_supplements(run_id)}


@router.get('/api/runs/{run_id}/supplements/{supplement_id}')
def supplement_detail(run_id: str, supplement_id: str):
    return detail(run_id, supplement_id)


@router.get('/api/runs/{run_id}/effective')
def effective_view(run_id: str):
    return effective(run_id)


@router.get('/api/runs/{run_id}/release')
def release(run_id: str):
    return release_state(run_id)
