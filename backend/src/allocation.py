"""两线共用蒸汽总表的小时分摊：排程、补齐等待、拟合分摊与中断恢复。"""
import logging
from datetime import timedelta
from uuid import uuid4

from redis.exceptions import RedisError
from sqlalchemy import delete, insert, select

from .auth import DomainError
from .config import ALLOC_QUEUE, Session, redis, utc_now
from .constants import AllocState, Equipment, Line, ShiftState
from .models import AllocationLine, EquipmentRun, MasterMeterRead, ProductionShift, SharedAllocation
from .shift_work import audit, boundaries, get_runs, window_runtime
from .steam_model import (
    ALLOC_FEATURES, ALLOC_MAX_HISTORY, AllocationIndistinguishable, fit_allocation, fingerprint, split_total,
)

log = logging.getLogger('steam.worker')

HOUR = timedelta(hours=1)
LINES = (Line.RETORT, Line.READY_MEALS)
OTHER_LINE = {Line.RETORT: Line.READY_MEALS, Line.READY_MEALS: Line.RETORT}
LINE_LABEL = {Line.RETORT: '杀菌线', Line.READY_MEALS: '调理线'}


def queue_hint(allocation_id):
    try:
        redis.rpush(ALLOC_QUEUE, str(allocation_id))
    except RedisError:
        # Worker also polls committed SQL rows when delivery is unavailable.
        return


def add_master_reading(db, data, user):
    reading_at = data.reading_at
    if db.scalar(select(MasterMeterRead.id).where(MasterMeterRead.reading_at == reading_at)):
        raise DomainError('reading_exists', '该时点已有总表读数，总表同一时点只抄一次')
    previous = db.scalar(select(MasterMeterRead).where(MasterMeterRead.reading_at < reading_at)
                         .order_by(MasterMeterRead.reading_at.desc()).limit(1))
    following = db.scalar(select(MasterMeterRead).where(MasterMeterRead.reading_at > reading_at)
                          .order_by(MasterMeterRead.reading_at).limit(1))
    if ((previous and previous.cumulative_kg > data.cumulative_kg) or
            (following and following.cumulative_kg < data.cumulative_kg)):
        raise DomainError('meter_reversed', '总表累计读数与相邻读数倒挂，请核对现场表盘')
    row = MasterMeterRead(**data.model_dump(), recorded_by=user.id)
    db.add(row)
    db.flush()
    audit(db, user.username, 'meter.master_recorded', row.id,
          reading_at=reading_at.isoformat(timespec='minutes'), source=row.source_ref)
    return row


def covering_shift(db, line, window_start):
    return db.scalar(select(ProductionShift).where(
        ProductionShift.line == line, ProductionShift.starts_at <= window_start,
        ProductionShift.ends_at > window_start).limit(1))


def insert_ignore():
    return insert(SharedAllocation).prefix_with('IGNORE', dialect='mysql').prefix_with('OR IGNORE', dialect='sqlite')


def ensure_allocation(db, window_start):
    """按小时窗口建立分摊任务；并发封账命中唯一约束时只保留先到的任务。"""
    created = db.execute(insert_ignore().values(window_start=window_start, state=AllocState.WAITING)).rowcount == 1
    alloc = db.scalar(select(SharedAllocation).where(SharedAllocation.window_start == window_start).with_for_update())
    return created, alloc


def boundary_reads(db, window_start):
    rows = db.scalars(select(MasterMeterRead).where(
        MasterMeterRead.reading_at.in_([window_start, window_start + HOUR])))
    return {row.reading_at: row for row in rows}


def readiness_blocker(db, window_start):
    """返回阻碍分摊的原因；None 表示两线启停与总表读数齐备。"""
    for line in LINES:
        shift = covering_shift(db, line, window_start)
        if shift is None or shift.state != ShiftState.SEALED:
            return f'等待{LINE_LABEL[line]}班次封账，启停记录以封账为准'
    missing = [slot.isoformat(timespec='minutes') for slot in (window_start, window_start + HOUR)
               if slot not in boundary_reads(db, window_start)]
    if missing:
        return f'缺少总表 {"、".join(missing)} 读数'
    return None


def refresh_allocation(db, alloc):
    """等待中的分摊在数据补齐后入队；已入队或已完成的保持原状。"""
    if alloc.state != AllocState.WAITING:
        return False
    blocker = readiness_blocker(db, alloc.window_start)
    if blocker:
        alloc.reason = blocker
        return False
    alloc.reason = None
    alloc.state = AllocState.QUEUED
    return True


def sync_shift_windows(db, shift, user):
    """班次封账后，为与另一线重叠的每个小时建立并刷新分摊，返回新入队的任务号。"""
    queued = []
    for window_start in boundaries(shift)[:-1]:
        if covering_shift(db, OTHER_LINE[shift.line], window_start) is None:
            continue
        created, alloc = ensure_allocation(db, window_start)
        if created:
            audit(db, user.username, 'alloc.scheduled', alloc.id, window=window_start.isoformat(timespec='minutes'))
        if refresh_allocation(db, alloc):
            audit(db, user.username, 'alloc.queued', alloc.id, window=window_start.isoformat(timespec='minutes'))
            queued.append(alloc.id)
    return queued


def sync_reading_windows(db, reading_at, user):
    """总表读数落账后刷新相邻两个小时的等待分摊，返回新入队的任务号。"""
    queued = []
    for window_start in (reading_at - HOUR, reading_at):
        alloc = db.scalar(select(SharedAllocation).where(SharedAllocation.window_start == window_start).with_for_update())
        if alloc and refresh_allocation(db, alloc):
            audit(db, user.username, 'alloc.queued', alloc.id, window=window_start.isoformat(timespec='minutes'))
            queued.append(alloc.id)
    return queued


def hour_features(runs, start, end):
    runtime = window_runtime(runs, start, end)
    return [runtime[Equipment.RETORT], runtime[Equipment.CIP], runtime[Equipment.BLANCHER]]


def gather_history(db, window_start):
    """目标小时之前、两线均封账且总表读数齐备的同小时样本，最近优先，最多 ALLOC_MAX_HISTORY 个。"""
    readings = list(db.scalars(select(MasterMeterRead).where(MasterMeterRead.reading_at <= window_start)
                               .order_by(MasterMeterRead.reading_at.desc()).limit(2 * ALLOC_MAX_HISTORY + 8)))
    by_time = {row.reading_at: row for row in readings}
    horizon = window_start - timedelta(days=45)
    shifts = {line: list(db.scalars(select(ProductionShift).where(
        ProductionShift.line == line, ProductionShift.state == ShiftState.SEALED,
        ProductionShift.starts_at < window_start, ProductionShift.ends_at > horizon))) for line in LINES}
    run_map = {}
    shift_ids = [shift.id for items in shifts.values() for shift in items]
    if shift_ids:
        for run in db.scalars(select(EquipmentRun).where(EquipmentRun.shift_id.in_(shift_ids))):
            run_map.setdefault(run.shift_id, []).append(run)
    rows = []
    for reading in readings:
        start = reading.reading_at
        if start >= window_start or start + HOUR not in by_time:
            continue
        pair = {}
        for line in LINES:
            shift = next((item for item in shifts[line] if item.starts_at <= start < item.ends_at), None)
            if shift is None:
                break
            pair[line] = hour_features(run_map.get(shift.id, []), start, start + HOUR)
        else:
            kg = by_time[start + HOUR].cumulative_kg - reading.cumulative_kg
            if kg >= 0:
                rows.append({'window': start.isoformat(timespec='minutes'),
                             'xa': pair[Line.RETORT], 'xb': pair[Line.READY_MEALS], 'kg': float(kg)})
        if len(rows) >= ALLOC_MAX_HISTORY:
            break
    rows.reverse()
    return rows


def compute_allocation(db, alloc):
    """汇总目标小时的冻结输入并计算分摊结果；只读，不写库。"""
    window_start = alloc.window_start
    window_end = window_start + HOUR
    shifts = {}
    for line in LINES:
        shift = covering_shift(db, line, window_start)
        if shift is None or shift.state != ShiftState.SEALED:
            return {'status': AllocState.WAITING, 'reason': f'等待{LINE_LABEL[line]}班次封账，启停记录以封账为准'}
        shifts[line] = shift
    reads = boundary_reads(db, window_start)
    if window_start not in reads or window_end not in reads:
        missing = [slot.isoformat(timespec='minutes') for slot in (window_start, window_end) if slot not in reads]
        return {'status': AllocState.WAITING, 'reason': f'缺少总表 {"、".join(missing)} 读数'}
    total = reads[window_end].cumulative_kg - reads[window_start].cumulative_kg
    if total < 0:
        raise ValueError('总表累计读数倒挂，无法分摊')
    features = {line: hour_features(get_runs(db, shifts[line].id), window_start, window_end) for line in LINES}
    history = gather_history(db, window_start)
    snapshot = {'window': window_start.isoformat(timespec='minutes'), 'history': history,
                'target': {'xa': features[Line.RETORT], 'xb': features[Line.READY_MEALS], 'kg': float(total)}}
    base = {'total_kg': total, 'snapshot': snapshot,
            'reads': {'start_read_id': reads[window_start].id, 'end_read_id': reads[window_end].id}}
    try:
        model = fit_allocation(history)
    except AllocationIndistinguishable as exc:
        return {**base, 'status': AllocState.TOTAL_ONLY, 'reason': str(exc), 'model': {'samples': len(history)}}
    share_a, share_b = split_total(model, features[Line.RETORT], features[Line.READY_MEALS], total)
    return {**base, 'status': AllocState.ALLOCATED, 'model': model,
            'shares': {Line.RETORT: share_a, Line.READY_MEALS: share_b}}


def apply_outcome(db, alloc, outcome):
    """把计算结果落库：两线估算在同一事务一起写入，相加等于当小时总表差值。"""
    status = outcome['status']
    if 'snapshot' in outcome:
        alloc.input_snapshot = outcome['snapshot']
        alloc.input_sha256 = fingerprint(outcome['snapshot'])
    if 'total_kg' in outcome:
        alloc.total_kg = outcome['total_kg']
    db.execute(delete(AllocationLine).where(AllocationLine.allocation_id == alloc.id))
    if status == AllocState.ALLOCATED:
        for line, kg in outcome['shares'].items():
            db.add(AllocationLine(allocation_id=alloc.id, line=line, estimated_kg=kg))
        alloc.reason = None
        alloc.result = {'features': ALLOC_FEATURES, 'model': outcome['model'],
                        'shares': {line: float(kg) for line, kg in outcome['shares'].items()},
                        'reads': outcome['reads']}
    else:
        alloc.reason = outcome.get('reason')
        if status == AllocState.TOTAL_ONLY:
            alloc.result = {'features': ALLOC_FEATURES, 'model': outcome['model'], 'reads': outcome['reads']}
    alloc.state = status
    alloc.lease_until = alloc.claim_token = None
    audit(db, 'worker', f'alloc.{status}', alloc.id, window=alloc.window_start.isoformat(timespec='minutes'),
          total_kg=float(outcome['total_kg']) if 'total_kg' in outcome else None, reason=outcome.get('reason'))


def claim():
    with Session.begin() as db:
        alloc = db.scalar(select(SharedAllocation).where(
            (SharedAllocation.state == AllocState.QUEUED) |
            ((SharedAllocation.state == AllocState.RUNNING) & (SharedAllocation.lease_until < utc_now())),
        ).order_by(SharedAllocation.id).limit(1).with_for_update(skip_locked=True))
        if alloc is None:
            return None
        alloc.state, alloc.claim_token = AllocState.RUNNING, uuid4().hex
        alloc.lease_until = utc_now() + timedelta(seconds=90)
        alloc.attempts += 1
        return alloc


def execute(claimed):
    outcome, error = None, None
    try:
        with Session() as db:
            outcome = compute_allocation(db, db.get(SharedAllocation, claimed.id))
    except Exception as exc:
        log.exception('allocation %s failed', claimed.id)
        error = (str(exc) or type(exc).__name__)[:240]
    with Session.begin() as db:
        alloc = db.get(SharedAllocation, claimed.id, with_for_update=True)
        if alloc.claim_token != claimed.claim_token or alloc.state != AllocState.RUNNING:
            return
        if error is None:
            apply_outcome(db, alloc, outcome)
        else:
            alloc.state, alloc.reason = AllocState.FAILED, error
            alloc.lease_until = alloc.claim_token = None
            audit(db, 'worker', 'alloc.failed', alloc.id, reason=error)
