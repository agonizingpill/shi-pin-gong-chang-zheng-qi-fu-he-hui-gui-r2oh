from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy import select, text

from . import allocation, planning, shift_work
from .auth import DomainError, allow, current_user, login
from .config import ARTIFACT_DIR, Session, engine, redis
from .constants import Role
from .models import AuditEntry, EnergyReview, LoadPlan, MasterMeterRead, ProductionShift, SharedAllocation
from .schemas import CloseReview, CorrectRun, Login, OpenShift, RecordMasterReading, RecordReading, RecordRun, RequestPlan, VerifySpike
from .views import allocation_view, fields, master_reading_view, meter_view, plan_view, review_view, run_view, shift_view, user_view

router = APIRouter()
writer = allow(Role.OPERATOR, Role.SUPERVISOR)
supervisor = allow(Role.SUPERVISOR)


@router.get('/health')
def health():
    with Session() as db:
        db.execute(select(ProductionShift.id).limit(1))
    redis.ping()
    return {'ok': True}


@router.post('/auth/login')
def sign_in(data: Login):
    token, user = login(data.username, data.password)
    return {'token': token, 'user': user_view(user)}


@router.get('/auth/me')
def whoami(user=Depends(current_user)):
    return user_view(user)


@router.get('/shifts')
def shifts(_user=Depends(current_user)):
    with Session() as db:
        rows = db.scalars(select(ProductionShift).order_by(ProductionShift.starts_at.desc()).limit(60))
        return [shift_view(db, shift) for shift in rows]


@router.post('/shifts', status_code=201)
def create_shift(data: OpenShift, user=Depends(writer)):
    lock_key = f'food-steam-load:roster:{data.line}'
    with engine.connect() as roster:
        locked = False
        try:
            with roster.begin():
                locked = roster.scalar(text('SELECT GET_LOCK(:key, 5)'), {'key': lock_key}) == 1
                if not locked:
                    raise DomainError('roster_busy', '排班簿正被更新，请稍后重试', 503)
                with Session(bind=roster, join_transaction_mode='rollback_only') as db:
                    result = shift_view(db, shift_work.create_shift(db, data, user), detail=True)
                    db.flush()
            return result
        finally:
            if locked:
                roster.execute(text('SELECT RELEASE_LOCK(:key)'), {'key': lock_key})


@router.get('/shifts/{shift_id}')
def show_shift(shift_id: int, _user=Depends(current_user)):
    with Session() as db:
        return shift_view(db, shift_work.get_shift(db, shift_id), detail=True)


@router.post('/shifts/{shift_id}/readings', status_code=201)
def record_reading(shift_id: int, data: RecordReading, user=Depends(writer)):
    with Session.begin() as db:
        shift = shift_work.get_shift(db, shift_id, lock=True)
        return meter_view(shift_work.add_reading(db, shift, data, user))


@router.post('/shifts/{shift_id}/equipment', status_code=201)
def record_run(shift_id: int, data: RecordRun, user=Depends(writer)):
    with Session.begin() as db:
        shift = shift_work.get_shift(db, shift_id, lock=True)
        return run_view(shift_work.save_run(db, shift, data, user))


@router.patch('/shifts/{shift_id}/equipment/{run_id}')
def correct_run(shift_id: int, run_id: int, data: CorrectRun, user=Depends(supervisor)):
    with Session.begin() as db:
        shift = shift_work.get_shift(db, shift_id, lock=True)
        return run_view(shift_work.save_run(db, shift, data, user, run_id))


@router.post('/shifts/{shift_id}/seal')
def handover(shift_id: int, user=Depends(writer)):
    with Session.begin() as db:
        shift = shift_work.get_shift(db, shift_id, lock=True)
        result = shift_view(db, shift_work.seal_shift(db, shift, user))
        queued = allocation.sync_shift_windows(db, shift, user)
    for allocation_id in queued:
        allocation.queue_hint(allocation_id)
    return result


@router.get('/plans')
def plans(_user=Depends(current_user)):
    with Session() as db:
        rows = db.scalars(select(LoadPlan).order_by(LoadPlan.id.desc()).limit(50))
        return [plan_view(db, row) for row in rows]


@router.post('/plans', status_code=202)
def enqueue_plan(data: RequestPlan, user=Depends(writer)):
    with Session.begin() as db:
        plan = planning.request_plan(db, data.shift_id, data.title, user)
        result = plan_view(db, plan)
    planning.queue_hint(plan.id)
    return result


@router.get('/plans/{plan_id}')
def show_plan(plan_id: int, _user=Depends(current_user)):
    with Session() as db:
        return plan_view(db, planning.get_plan(db, plan_id))


@router.get('/plans/{plan_id}/evidence')
def evidence(plan_id: int, _user=Depends(current_user)):
    with Session() as db:
        plan = planning.get_plan(db, plan_id)
        if not plan.evidence_file or not (ARTIFACT_DIR / plan.evidence_file).is_file():
            raise DomainError('evidence_unavailable', '估计证据尚未生成', 404)
        return FileResponse(ARTIFACT_DIR / plan.evidence_file, media_type='application/json',
                            filename=f'steam-plan-{plan.id}.json')


@router.post('/plans/{plan_id}/approve', status_code=201)
def approve(plan_id: int, user=Depends(supervisor)):
    with Session.begin() as db:
        plan = planning.get_plan(db, plan_id, lock=True)
        return review_view(db, planning.approve_plan(db, plan, user))


@router.get('/reviews')
def reviews(_user=Depends(current_user)):
    with Session() as db:
        rows = db.scalars(select(EnergyReview).order_by(EnergyReview.id.desc()).limit(50))
        return [review_view(db, row) for row in rows]


@router.get('/reviews/{review_id}')
def show_review(review_id: int, _user=Depends(current_user)):
    with Session() as db:
        return review_view(db, planning.get_review(db, review_id))


@router.post('/reviews/{review_id}/spikes/{spike_id}/verify')
def mark_spike(review_id: int, spike_id: int, data: VerifySpike, user=Depends(supervisor)):
    with Session.begin() as db:
        review = planning.get_review(db, review_id, lock=True)
        planning.verify_spike(db, review, spike_id, data, user)
        return review_view(db, review)


@router.post('/reviews/{review_id}/close')
def close(review_id: int, data: CloseReview, user=Depends(supervisor)):
    with Session.begin() as db:
        review = planning.get_review(db, review_id, lock=True)
        return review_view(db, planning.close_review(db, review, data, user))


@router.get('/master-readings')
def master_readings(_user=Depends(current_user)):
    with Session() as db:
        rows = db.scalars(select(MasterMeterRead).order_by(MasterMeterRead.reading_at.desc()).limit(96))
        return [master_reading_view(row) for row in rows]


@router.post('/master-readings', status_code=201)
def record_master_reading(data: RecordMasterReading, user=Depends(writer)):
    with Session.begin() as db:
        row = allocation.add_master_reading(db, data, user)
        queued = allocation.sync_reading_windows(db, row.reading_at, user)
        result = master_reading_view(row)
    for allocation_id in queued:
        allocation.queue_hint(allocation_id)
    return result


@router.get('/allocations')
def allocations(_user=Depends(current_user)):
    with Session() as db:
        rows = db.scalars(select(SharedAllocation).order_by(SharedAllocation.window_start.desc()).limit(72))
        return [allocation_view(db, row) for row in rows]


@router.get('/allocations/{allocation_id}')
def show_allocation(allocation_id: int, _user=Depends(current_user)):
    with Session() as db:
        alloc = db.get(SharedAllocation, allocation_id)
        if alloc is None:
            raise DomainError('allocation_not_found', '找不到该小时的分摊记录', 404)
        return allocation_view(db, alloc, detail=True)


@router.get('/audit')
def audit_entries(_user=Depends(supervisor)):
    with Session() as db:
        rows = db.scalars(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(100))
        return [fields(row, 'id actor action object_ref details at') for row in rows]
