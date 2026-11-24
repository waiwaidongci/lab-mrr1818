import hashlib
import json
import time

from fastapi import HTTPException
from sqlalchemy import insert, select, update

from src.constants.enums import State
from src.models.database import add_event, engine, fetch_one
from src.models.tables import accounting_mutex, adjustment_batches, datasets, period_locks, runs
from src.services.domain import (DEFAULT_MONTH, _month_bounds, closing_states_for_rows,
                                 compute, compute_carryforward, next_month, previous_month)


def validate_month(month):
    try:
        _month_bounds(month)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return month


def init_accounting():
    """Create the singleton mutex row used to serialize close/post operations."""
    now = time.time()
    with engine.begin() as connection:
        if fetch_one(connection, accounting_mutex, 1) is None:
            connection.execute(insert(accounting_mutex).values(id=1, version=0, touched_at=now))


def _acquire_mutex(connection, at):
    # The first write takes a row write lock on PostgreSQL and a database write
    # lock on SQLite.  Every close and batch-post transaction uses this same
    # row, so a batch's target month is chosen exactly once and stays whole.
    changed = connection.execute(
        update(accounting_mutex)
        .where(accounting_mutex.c.id == 1)
        .values(version=accounting_mutex.c.version + 1, touched_at=at)
    )
    if changed.rowcount != 1:
        connection.execute(insert(accounting_mutex).values(id=1, version=1, touched_at=at))


def first_open_month(connection):
    locked = set(connection.scalars(select(period_locks.c.month)).all())
    if not locked:
        return None
    candidate = min(locked)
    while candidate in locked:
        candidate = next_month(candidate)
    return candidate


def batch_checksum(rows):
    canonical = sorted(rows, key=lambda row: (
        row.get('effective_date', ''), row.get('subscription_id', ''),
        row.get('account_id', ''), row.get('tier', ''), row.get('status', ''),
        str(row.get('mrr_minor', '')),
    ))
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False,
                         separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def _fetch_lock(connection, month):
    return connection.execute(
        select(period_locks).where(period_locks.c.month == month)
    ).mappings().one_or_none()


def _public_batch(row):
    return {
        'id': row['id'],
        'target_month': row['target_month'],
        'month': row['target_month'],
        'accounting_month': row['target_month'],
        'name': row['name'],
        'idempotency_key': row['idempotency_key'],
        'checksum': row['checksum'],
        'record_count': row['record_count'],
        'actor': row['actor'],
        'created': row['created'],
    }


def _public_lock(row):
    return {
        'month': row['month'],
        'run_id': row['run_id'],
        'dataset_id': row['dataset_id'],
        'actor': row['actor'],
        'locked_at': row['locked_at'],
    }


def _finalize_report(report):
    report['columns'] = list(dict.fromkeys(key for row in report['rows'] for key in row))
    report['metric_order'] = list(report['metrics'])
    return report


def create_supplement_batch(rows, actor, name='迟到补传', requested_month=None,
                            idempotency_key=None):
    if not rows:
        raise HTTPException(422, '补传文件需要至少一条记录。')
    if requested_month:
        validate_month(requested_month)
    checksum = batch_checksum(rows)
    now = time.time()
    with engine.begin() as connection:
        _acquire_mutex(connection, now)
        if idempotency_key:
            same_key = connection.execute(
                select(adjustment_batches).where(
                    adjustment_batches.c.idempotency_key == idempotency_key)
            ).mappings().one_or_none()
            if same_key is not None:
                if same_key['checksum'] != checksum:
                    raise HTTPException(409, '幂等键已用于另一批补传内容，请更换批次编号。')
                return {**_public_batch(same_key), 'created': False}
        existing = connection.execute(
            select(adjustment_batches).where(adjustment_batches.c.checksum == checksum)
        ).mappings().one_or_none()
        if existing is not None:
            return {**_public_batch(existing), 'created': False}

        open_month = first_open_month(connection)
        if requested_month and open_month and requested_month != open_month:
            raise HTTPException(
                409,
                f'当前首个未锁月是 {open_month}；并发关账后，历史补传只能整批计入该月。'
            )
        target_month = requested_month or open_month or DEFAULT_MONTH

        batch_id = connection.execute(insert(adjustment_batches).values(
            target_month=target_month, name=name.strip()[:180] or '迟到补传',
            idempotency_key=idempotency_key, checksum=checksum, records=rows,
            record_count=len(rows), actor=actor, created=now
        )).inserted_primary_key[0]
        add_event(connection, actor, 'supplement', batch_id, now)
        row = fetch_one(connection, adjustment_batches, batch_id)
        return {**_public_batch(row), 'created': True}


def list_supplement_batches(month=None):
    statement = select(adjustment_batches).order_by(adjustment_batches.c.id.desc()).limit(100)
    if month:
        statement = statement.where(adjustment_batches.c.target_month == month)
    with engine.connect() as connection:
        return [_public_batch(row) for row in connection.execute(statement).mappings()]


def list_period_locks():
    with engine.connect() as connection:
        rows = connection.execute(
            select(period_locks).order_by(period_locks.c.month.desc()).limit(100)
        ).mappings()
        return [_public_lock(row) for row in rows]


def open_month_info():
    with engine.connect() as connection:
        month = first_open_month(connection) or DEFAULT_MONTH
    return {'first_open_month': month, 'month': month}


def lock_period(month, actor, run_id=None, dataset_id=None):
    validate_month(month)
    now = time.time()
    with engine.begin() as connection:
        _acquire_mutex(connection, now)

        run = None
        if run_id is not None:
            run = fetch_one(connection, runs, run_id)
            if run is None:
                raise HTTPException(404, '分析任务不存在。')
            if run['state'] != State.completed.value or not run['result']:
                raise HTTPException(409, '收入桥计算完成后才能锁账。')
            if run['result'].get('can_publish') is False:
                raise HTTPException(409, '对账未平的收入桥不能锁账。')
            run_month = (run['options'] or {}).get('report_month')
            if run_month != month:
                raise HTTPException(422, f'分析任务的报告月是 {run_month}，与锁账月份 {month} 不一致。')
            dataset_id = run['dataset_id']

        if run is None and dataset_id is None:
            candidates = connection.execute(
                select(runs).where(runs.c.state == State.completed.value)
                .order_by(runs.c.id.desc()).limit(100)
            ).mappings().all()
            run = next((item for item in candidates
                         if (item['options'] or {}).get('report_month') == month
                         and item['result'] and item['result'].get('can_publish') is not False), None)
            if run is not None:
                run_id = run['id']
                dataset_id = run['dataset_id']

        existing = _fetch_lock(connection, month)
        if existing is not None:
            if run_id is not None and existing['run_id'] not in (None, run_id):
                raise HTTPException(409, f'{month} 已由分析任务 #{existing["run_id"]} 关账，已锁收入桥不可改写。')
            return {**_public_lock(existing), 'locked': True, 'created': False}

        previous = _fetch_lock(connection, previous_month(month))
        batches = connection.execute(
            select(adjustment_batches)
            .where(adjustment_batches.c.target_month == month)
            .order_by(adjustment_batches.c.id)
        ).mappings().all()

        if previous is not None:
            base_rows = list(previous['records'])
        else:
            if run is not None:
                dataset_id = run['dataset_id']
            if dataset_id is None:
                raise HTTPException(409, '首次锁账请从已完成且对账平衡的收入桥分析任务进入。')
            dataset = fetch_one(connection, datasets, dataset_id)
            if dataset is None:
                raise HTTPException(404, '数据版本不存在。')
            base_rows = list(dataset['records'])

        rows = list(base_rows)
        supplement_rows = []
        for batch in batches:
            rows.extend(batch['records'])
            supplement_rows.extend(batch['records'])

        options = {'report_month': month}
        try:
            if previous is not None:
                report = compute_carryforward(rows, options, previous['closing_states'],
                                              supplement_rows)
            else:
                report = compute(rows, options)
            closing_states = closing_states_for_rows(rows, month)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        report = _finalize_report(report)
        if not report.get('can_publish'):
            raise HTTPException(409, '补传后对账不平，不能锁账；请修正补传记录后重跑。')

        connection.execute(insert(period_locks).values(
            month=month, run_id=run_id, dataset_id=dataset_id, actor=actor, locked_at=now,
            records=rows, closing_states=closing_states, snapshot=report
        ))
        add_event(connection, actor, 'lock', run_id if run_id is not None else 0, now)
        return {'month': month, 'run_id': run_id, 'dataset_id': dataset_id,
                'actor': actor, 'locked_at': now, 'locked': True, 'created': True}


def lock_run(run_id, actor):
    with engine.connect() as connection:
        run = fetch_one(connection, runs, run_id)
    if run is None:
        raise HTTPException(404, '分析任务不存在。')
    month = (run['options'] or {}).get('report_month')
    validate_month(month)
    return lock_period(month, actor, run_id)


def period_status(month):
    validate_month(month)
    with engine.connect() as connection:
        lock = _fetch_lock(connection, month)
        previous = _fetch_lock(connection, previous_month(month))
        open_month = first_open_month(connection)
        batches = connection.execute(
            select(adjustment_batches).where(adjustment_batches.c.target_month == month)
            .order_by(adjustment_batches.c.id.desc()).limit(100)
        ).mappings().all()
    return {
        'month': month,
        'locked': lock is not None,
        'lock': _public_lock(lock) if lock is not None else None,
        'first_open_month': open_month,
        'next_month': next_month(month),
        'previous_month': previous_month(month),
        'previous_locked': previous is not None,
        'bridge_available': lock is not None or previous is not None,
        'batches': [_public_batch(batch) for batch in batches],
    }


def period_bridge(month):
    validate_month(month)
    with engine.connect() as connection:
        lock = _fetch_lock(connection, month)
        if lock is not None:
            locked_batches = connection.execute(
                select(adjustment_batches).where(adjustment_batches.c.target_month == month)
                .order_by(adjustment_batches.c.id.desc()).limit(100)
            ).mappings().all()
            report = dict(lock['snapshot'])
            return {
                **report,
                'month': month,
                'period': month,
                'locked': True,
                'lock': _public_lock(lock),
                'batches': [_public_batch(batch) for batch in locked_batches],
            }

        previous = _fetch_lock(connection, previous_month(month))
        if previous is None:
            raise HTTPException(409, f'{month} 的上月尚未锁账，暂无可承接的已锁期末。')
        batches = connection.execute(
            select(adjustment_batches)
            .where(adjustment_batches.c.target_month == month)
            .order_by(adjustment_batches.c.id)
        ).mappings().all()
        rows = list(previous['records'])
        locked_states = previous['closing_states']
        supplement_rows = []
        batch_info = []
        for batch in batches:
            rows.extend(batch['records'])
            supplement_rows.extend(batch['records'])
            batch_info.append(_public_batch(batch))

    try:
        report = compute_carryforward(rows, {'report_month': month},
                                      locked_states, supplement_rows)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    report = _finalize_report(report)
    return {**report, 'month': month, 'locked': False, 'lock': None, 'batches': batch_info}


# Vocabulary aliases for callers and focused concurrency/idempotency tests.
post_supplement_batch = create_supplement_batch
post_adjustment_batch = create_supplement_batch
apply_adjustment_batch = create_supplement_batch
close_period = lock_period
get_period_bridge = period_bridge
