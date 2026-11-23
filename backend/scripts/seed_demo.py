"""Five factory shifts per line, with one physical meter surge retained in the logbook.

LINE-A and LINE-B run overlapping shifts that share the plant master steam meter, so
hourly master readings and shared allocations are seeded as well. LINE-B's last shift
is left open to demonstrate allocations waiting for the peer side to seal.
"""
import logging
from datetime import UTC, datetime, timedelta, timezone
from random import Random
from uuid import uuid4

from sqlalchemy import select

from src import allocation
from src.auth import password_hash
from src.config import Session
from src.constants import AllocState, Equipment, Line, PlanState, Role, ShiftState
from src.models import EquipmentRun, LoadPlan, MasterMeterRead, ProductionShift, SharedAllocation, SteamMeter, User
from src.planning import approve_plan, request_plan
from src.shift_work import capture_rows
from src.steam_model import estimate
from src.worker import write_evidence

ACCOUNTS = [
    ('operator', '周岚', Role.OPERATOR, 'SteamOps1064!'),
    ('supervisor', '陆师傅', Role.SUPERVISOR, 'SteamLead1064!'),
    ('viewer', '梁敏', Role.VIEWER, 'SteamView1064!'),
]


def seed_shifts(db, operator):
    rng = Random(1064)
    fixtures = [('A-1117-B', '菌菇炖汁', 17, 16.5), ('A-1118-B', '菌菇炖汁', 18, 18.0),
                ('A-1119-B', '番茄豆罐头', 19, 14.0), ('A-1120-B', '番茄豆罐头', 20, 16.0),
                ('A-1121-B', '菌菇炖汁', 21, 15.5)]
    ready_products = ['黑椒牛柳调理包', '宫保鸡丁调理包', '咖喱鸡肉调理包', '鱼香肉丝调理包', '黑椒牛柳调理包']
    master_delta = {}
    last = None
    for index, (code, product, day, ambient) in enumerate(fixtures):
        start = datetime(2026, 11, day, 14)
        specs = []
        increments = []
        for hour in range(8):
            retort = rng.randrange(18, 53)
            cip = rng.randrange(10, 30) if (hour + index) % 3 == 1 else 0
            blancher = rng.randrange(4, 32)
            specs.append((retort, cip, blancher))
            increments.append(round(77 + 5.8 * retort + 3.3 * cip + 0.15 * blancher + rng.uniform(-7, 7)
                                    + (245 if index == 2 and hour == 4 else 0), 2))
        peer_specs = []
        peer_increments = []
        for hour in range(8):
            retort = rng.randrange(12, 46)
            cip = rng.randrange(8, 26) if (hour + index) % 2 == 0 else 0
            blancher = rng.randrange(6, 28)
            peer_specs.append((retort, cip, blancher))
            peer_increments.append(round(58 + 4.6 * retort + 2.9 * cip + 0.11 * blancher + rng.uniform(-6, 6), 2))
        for hour in range(8):
            master_delta[start + timedelta(hours=hour)] = round(increments[hour] + peer_increments[hour], 2)
        shift = db.scalar(select(ProductionShift).where(ProductionShift.code == code))
        if shift is None:
            end = start + timedelta(hours=8)
            handover_utc = end.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(UTC).replace(tzinfo=None)
            shift = ProductionShift(code=code, line=Line.RETORT, product=product, batch_code=f'QH-2611{day}-B',
                                    starts_at=start, ends_at=end, ambient_c=ambient,
                                    state=ShiftState.SEALED, opened_by=operator.id, sealed_at=handover_utc)
            db.add(shift)
            db.flush()
            cumulative = 460200 + 2700 * index
            db.add(SteamMeter(shift_id=shift.id, reading_at=start, cumulative_kg=cumulative,
                             source_ref=f'蒸汽交接簿-{day}日-B班 / 14:00', recorded_by=operator.id))
            for hour, (retort, cip, blancher) in enumerate(specs):
                window = start + timedelta(hours=hour)
                for asset, offset, length in zip(Equipment, [1, 4, 7], [retort, cip, blancher]):
                    if length:
                        db.add(EquipmentRun(shift_id=shift.id, asset=asset, starts_at=window + timedelta(minutes=offset),
                                            ends_at=window + timedelta(minutes=offset + length), logged_by=operator.id))
                cumulative = round(cumulative + increments[hour], 2)
                label = ' / 尖峰原表照片编号 P-19-1900' if index == 2 and hour == 4 else ''
                db.add(SteamMeter(shift_id=shift.id, reading_at=window + timedelta(hours=1), cumulative_kg=cumulative,
                                  source_ref=f'蒸汽交接簿-{day}日-B班 / {15 + hour}:00{label}', recorded_by=operator.id))
            db.flush()
            capture_rows(db, shift)
        last = shift
        peer_code = f'B-11{day}-B'
        if db.scalar(select(ProductionShift).where(ProductionShift.code == peer_code)) is None:
            end = start + timedelta(hours=8)
            peer_open = index == len(fixtures) - 1
            handover_utc = end.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(UTC).replace(tzinfo=None)
            peer = ProductionShift(code=peer_code, line=Line.READY_MEALS, product=ready_products[index],
                                   batch_code=f'QH-2611{day}-C', starts_at=start, ends_at=end, ambient_c=ambient,
                                   state=ShiftState.OPEN if peer_open else ShiftState.SEALED,
                                   opened_by=operator.id, sealed_at=None if peer_open else handover_utc)
            db.add(peer)
            db.flush()
            covered = 3 if peer_open else 8
            cumulative = 288000 + 1900 * index
            db.add(SteamMeter(shift_id=peer.id, reading_at=start, cumulative_kg=cumulative,
                             source_ref=f'调理线抄表卡-{day}日-B班 / 14:00', recorded_by=operator.id))
            for hour, (retort, cip, blancher) in enumerate(peer_specs[:covered]):
                window = start + timedelta(hours=hour)
                for asset, offset, length in zip(Equipment, [2, 6, 9], [retort, cip, blancher]):
                    if length:
                        db.add(EquipmentRun(shift_id=peer.id, asset=asset, starts_at=window + timedelta(minutes=offset),
                                            ends_at=window + timedelta(minutes=offset + length), logged_by=operator.id))
                cumulative = round(cumulative + peer_increments[hour], 2)
                db.add(SteamMeter(shift_id=peer.id, reading_at=window + timedelta(hours=1), cumulative_kg=cumulative,
                                  source_ref=f'调理线抄表卡-{day}日-B班 / {15 + hour}:00', recorded_by=operator.id))
            db.flush()
            if not peer_open:
                capture_rows(db, peer)
    cumulative = 905000
    for window in sorted(master_delta):
        day, hour = window.day, window.hour
        if not db.scalar(select(MasterMeterRead.id).where(MasterMeterRead.reading_at == window)):
            db.add(MasterMeterRead(reading_at=window, cumulative_kg=cumulative,
                                  source_ref=f'蒸汽总表交接簿-{day}日 / {hour}:00', recorded_by=operator.id))
        cumulative = round(cumulative + master_delta[window], 2)
        if not db.scalar(select(MasterMeterRead.id).where(MasterMeterRead.reading_at == window + timedelta(hours=1))):
            db.add(MasterMeterRead(reading_at=window + timedelta(hours=1), cumulative_kg=cumulative,
                                  source_ref=f'蒸汽总表交接簿-{day}日 / {hour + 1}:00', recorded_by=operator.id))
    db.flush()
    return last


def seed_allocations(db, operator):
    """为两线重叠的共用小时排程分摊，并直接演算已齐备的任务。"""
    for shift in db.scalars(select(ProductionShift).where(ProductionShift.state == ShiftState.SEALED)):
        allocation.sync_shift_windows(db, shift, operator)
    pending = db.scalars(select(SharedAllocation).where(SharedAllocation.state == AllocState.QUEUED)
                         .order_by(SharedAllocation.window_start))
    for alloc in pending:
        allocation.apply_outcome(db, alloc, allocation.compute_allocation(db, alloc))


def seed():
    with Session.begin() as db:
        users = {}
        for name, display_name, role, password in ACCOUNTS:
            user = db.scalar(select(User).where(User.username == name))
            if user is None:
                user = User(username=name, display_name=display_name, role=role, password_hash=password_hash(password))
                db.add(user)
                db.flush()
            users[role] = user
        shift = seed_shifts(db, users[Role.OPERATOR])
        seed_allocations(db, users[Role.OPERATOR])
        if db.scalar(select(LoadPlan.id).where(LoadPlan.shift_id == shift.id).limit(1)):
            return
        plan = request_plan(db, shift.id, '晚班供汽基线 · 菌菇炖汁', users[Role.OPERATOR])
        plan.result = estimate(plan.input_snapshot)
        plan.result = {**plan.result, 'input_sha256': plan.input_sha256}
        plan.state = PlanState.ESTIMATED
        plan.evidence_file = f'plan-{plan.id}-{uuid4().hex}.json'
        write_evidence(plan.evidence_file, plan.result)
        approve_plan(db, plan, users[Role.SUPERVISOR])


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    seed()
    logging.info('demo seed complete')
