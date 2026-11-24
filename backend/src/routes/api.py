from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Response

from src.config.settings import ARTIFACTS
from src.constants.enums import Role
from src.controllers.query import delivery_status, jobs, recent_activity, require_row, revisions
from src.models.tables import datasets, runs
from src.schemas.requests import Delivery, Import, LockPeriod, Login, Review, Run, Supplement
from src.services.accounting import (create_supplement_batch, list_period_locks,
                                     list_supplement_batches, lock_period, lock_run,
                                     open_month_info, period_bridge, period_status)
from src.services.data import PROFILE, csv_records, save_revision
from src.services.domain import sample_rows
from src.services.workflow import add_run, cancel_schedule, review_run, schedule_report
from src.utils.exports import write_csv
from src.utils.security import allowed, authenticate, signed_in

router = APIRouter()
analyst = allowed(Role.analyst.value)
reviewer = allowed(Role.reviewer.value)


@router.post('/login')
def login(body: Login):
    return authenticate(body.username, body.password)


@router.get('/me')
def current(account=Depends(signed_in)):
    return dict(account)


@router.get('/profile')
def profile():
    return PROFILE


def download_csv(data, name):
    return Response(data, media_type='text/csv; charset=utf-8',
                    headers={'content-disposition': "attachment; filename*=UTF-8''" + quote(name)})


@router.get('/example.csv')
def example(account=Depends(signed_in)):
    return download_csv(write_csv(sample_rows(), [c['key'] for c in PROFILE['columns']], False), PROFILE['filename'])


@router.get('/datasets')
def list_datasets(account=Depends(signed_in)):
    return revisions()


@router.get('/datasets/{key}')
def get_dataset(key: int, account=Depends(signed_in)):
    dataset = require_row(datasets, key, '数据版本')
    records = dataset.pop('records')
    return {**dataset, 'count': len(records), 'records': records[:100]}


@router.post('/datasets', status_code=201)
def import_data(body: Import, account=Depends(analyst)):
    rows = csv_records(body.csv)
    key, created = save_revision(body.name, rows, account['username'])
    return {'id': key, 'created': created, 'rows': len(rows)}


@router.get('/runs')
def list_runs(account=Depends(signed_in)):
    return jobs()


@router.post('/runs', status_code=202)
def queue(body: Run, account=Depends(analyst)):
    return add_run(body, account['username'])


@router.get('/runs/{key}')
def result(key: int, account=Depends(signed_in)):
    return require_row(runs, key, '分析任务')


@router.post('/runs/{key}/review')
def review(key: int, body: Review, account=Depends(reviewer)):
    return review_run(key, body, account['username'])


@router.post('/runs/{key}/lock')
@router.post('/runs/{key}/close')
def lock_analysis_run(key: int, account=Depends(reviewer)):
    return lock_run(key, account['username'])


@router.post('/periods/{month}/lock')
@router.post('/periods/{month}/close')
@router.post('/accounting/periods/{month}/lock')
@router.post('/accounting/periods/{month}/close')
def lock_month(month: str, body: LockPeriod | None = None, account=Depends(reviewer)):
    return lock_period(month, account['username'],
                       body.run_id if body else None,
                       body.dataset_id if body else None)


# Common aliases used by period-close and adjustment-batch clients.
@router.post('/months/{month}/lock')
def lock_month_alias(month: str, body: LockPeriod | None = None, account=Depends(reviewer)):
    return lock_period(month, account['username'], body.run_id if body else None)


@router.post('/locks')
@router.post('/accounting/lock')
def create_lock(body: LockPeriod, account=Depends(reviewer)):
    month = body.month or body.report_month
    if month:
        return lock_period(month, account['username'], body.run_id, body.dataset_id)
    if body.run_id is None:
        raise HTTPException(422, '请提供要关账的分析任务或月份。')
    return lock_run(body.run_id, account['username'])


@router.post('/close')
@router.post('/accounting/close')
def close_period_alias(body: LockPeriod, account=Depends(reviewer)):
    return create_lock(body, account)


@router.get('/locks')
def get_locks(account=Depends(signed_in)):
    return list_period_locks()


@router.get('/locks/{month}')
def get_lock(month: str, account=Depends(signed_in)):
    status = period_status(month)
    if not status['locked']:
        raise HTTPException(404, f'{month} 尚未锁账。')
    return period_bridge(month)


@router.get('/accounting/open-month')
def accounting_open_month(account=Depends(signed_in)):
    return open_month_info()


@router.get('/accounting/status')
def accounting_status(month: str, account=Depends(signed_in)):
    return period_status(month)


@router.get('/accounting/bridge')
@router.get('/bridges')
def accounting_bridge(month: str, account=Depends(signed_in)):
    return period_bridge(month)


@router.get('/bridges/{month}')
def bridge_by_month(month: str, account=Depends(signed_in)):
    return period_bridge(month)


@router.get('/periods/{month}')
def period_status_route(month: str, account=Depends(signed_in)):
    return period_status(month)


@router.get('/periods/{month}/bridge')
def period_bridge_route(month: str, account=Depends(signed_in)):
    return period_bridge(month)


@router.get('/accounting/report.csv')
def accounting_report(month: str, account=Depends(signed_in)):
    report = period_bridge(month)
    if account['role'] == Role.viewer.value and not report.get('locked'):
        raise HTTPException(403, '只读账号可导出已锁账的收入桥。')
    data = write_csv(report['rows'], report.get('columns'))
    return download_csv(data, f"收入桥-{month}.csv")


@router.get('/supplements')
@router.get('/adjustments')
@router.get('/adjustment-batches')
def get_supplements(month: str | None = None, account=Depends(signed_in)):
    return list_supplement_batches(month)


@router.post('/supplements', status_code=201)
def add_supplement(body: Supplement, account=Depends(analyst),
                   idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    return _save_supplement(body, account['username'], idempotency_key)


@router.post('/adjustments', status_code=201)
@router.post('/adjustment-batches', status_code=201)
@router.post('/batches', status_code=201)
def add_adjustment(body: Supplement, account=Depends(analyst),
                   idempotency_key: str | None = Header(default=None, alias='Idempotency-Key')):
    return _save_supplement(body, account['username'], idempotency_key)


@router.post('/datasets/{key}/supplement', status_code=201)
def add_dataset_supplement(key: int, body: Supplement | None = None, account=Depends(analyst)):
    dataset = require_row(datasets, key, '数据版本')
    month = body.month or body.target_month if body else None
    return create_supplement_batch(dataset['records'], account['username'],
                                   dataset['name'], month)


def _save_supplement(body: Supplement, actor, header_key=None):
    requested_month = body.target_month or body.month
    idempotency_key = body.idempotency_key or header_key
    if body.csv:
        rows = csv_records(body.csv)
        name = body.name or '迟到补传'
    elif body.dataset_id:
        dataset = require_row(datasets, body.dataset_id, '数据版本')
        rows = dataset['records']
        name = body.name or dataset['name']
    else:
        raise HTTPException(422, '请提供补传 CSV 或已保存的数据版本编号。')
    return create_supplement_batch(rows, actor, name, requested_month, idempotency_key)


@router.post('/runs/{key}/schedules', status_code=201)
def schedule(key: int, body: Delivery, account=Depends(reviewer)):
    return schedule_report(key, body, account['username'])


@router.get('/delivery')
def delivery(account=Depends(signed_in)):
    return delivery_status()


@router.delete('/schedules/{key}')
def stop_schedule(key: int, account=Depends(reviewer)):
    return cancel_schedule(key, account['username'])


@router.get('/audit')
def activity(account=Depends(signed_in)):
    return recent_activity()


@router.get('/runs/{key}/report.csv')
def report(key: int, account=Depends(signed_in)):
    run = require_row(runs, key, '分析任务')
    if run['decision'] != 'approved' and account['role'] == Role.viewer.value:
        raise HTTPException(403, '只读账号可导出已批准的报告。')
    if not run['result']:
        raise HTTPException(409, '分析结果尚不可导出。')
    path = ARTIFACTS / f"run-{key}-{run['started'].hex()}.csv"
    data = path.read_bytes() if path.exists() else write_csv(run['result']['rows'], run['result'].get('columns'))
    return download_csv(data, f"{PROFILE['title']}-{key}.csv")
