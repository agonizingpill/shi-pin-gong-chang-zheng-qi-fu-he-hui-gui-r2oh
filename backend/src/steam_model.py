from hashlib import sha256
from json import dumps
from decimal import Decimal, ROUND_HALF_UP
import warnings

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.feature_selection import mutual_info_regression
from sklearn.linear_model import HuberRegressor
from sklearn.preprocessing import StandardScaler

FEATURES = ['杀菌釜运行分钟', 'CIP 清洗分钟', '漂烫机运行分钟', '设备启动次数', '环境温度 °C', '班内小时序号']
SEED = 1064
EPSILON = 1.35
MIN_SPIKE_KG = 60

ALLOC_FEATURES = ['杀菌釜运行分钟', 'CIP 清洗分钟', '漂烫机运行分钟']
ALLOC_MIN_HISTORY = 12
ALLOC_MAX_HISTORY = 60
ALLOC_COLLINEARITY = 0.98


class AllocationIndistinguishable(Exception):
    """历史工况无法区分两线各自用量，只能出总耗。"""


def fit_allocation(rows):
    """用历史同小时的两线启停与总耗拟合分摊模型；分不清各自用量时中止。"""
    if len(rows) < ALLOC_MIN_HISTORY:
        raise AllocationIndistinguishable(f'历史共用小时不足（{len(rows)}/{ALLOC_MIN_HISTORY}），无法拟合分摊')
    xa = np.array([row['xa'] for row in rows], dtype=float)
    xb = np.array([row['xb'] for row in rows], dtype=float)
    y = np.array([row['kg'] for row in rows], dtype=float)
    totals_a, totals_b = xa.sum(axis=1), xb.sum(axis=1)
    if np.ptp(totals_a) < 1e-9 or np.ptp(totals_b) < 1e-9:
        raise AllocationIndistinguishable('有产线在历史共用小时内工况无变化，无法区分各自用量')
    correlation = float(np.corrcoef(totals_a, totals_b)[0, 1])
    if abs(correlation) >= ALLOC_COLLINEARITY:
        raise AllocationIndistinguishable(f'两线工况同步变化（相关系数 {correlation:.3f}），无法区分各自用量')
    with warnings.catch_warnings():
        warnings.simplefilter('error', ConvergenceWarning)
        model = HuberRegressor(epsilon=EPSILON, alpha=0.0001, max_iter=1000).fit(np.hstack([xa, xb]), y)
    return {'coef_a': [round(float(value), 6) for value in model.coef_[:3]],
            'coef_b': [round(float(value), 6) for value in model.coef_[3:]],
            'intercept': round(float(model.intercept_), 4),
            'correlation': round(correlation, 4), 'samples': len(rows),
            'outliers': int(model.outliers_.sum())}


def split_total(model, xa, xb, total_kg):
    """按两线模型贡献的比例分摊总量；两线估算相加严格等于当小时总表差值。"""
    contrib_a = max(0.0, float(np.dot(model['coef_a'], xa)))
    contrib_b = max(0.0, float(np.dot(model['coef_b'], xb)))
    total = total_kg if isinstance(total_kg, Decimal) else Decimal(str(total_kg))
    if contrib_a + contrib_b <= 1e-9:
        share_a = (total / 2).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    else:
        ratio = Decimal(str(contrib_a / (contrib_a + contrib_b)))
        share_a = (total * ratio).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return share_a, total - share_a


def fingerprint(snapshot):
    return sha256(dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def train(rows):
    x = np.array([row['features'] for row in rows])
    y = np.array([row['kg'] for row in rows])
    usable = np.flatnonzero(np.ptp(x, axis=0) > 1e-9)
    if len(usable) == 0 or np.ptp(y) < 1e-9:
        raise ValueError('历史工况或耗汽量缺少变化，无法建立负荷基线')
    values = mutual_info_regression(x[:, usable], y, random_state=SEED, n_neighbors=3)
    ranking = sorted(zip(usable.tolist(), values.tolist()), key=lambda item: (-item[1], item[0]))
    selected = [i for i, _score in ranking[:3]]
    transform = StandardScaler().fit(x[:, selected])
    with warnings.catch_warnings():
        warnings.simplefilter('error', ConvergenceWarning)
        regressor = HuberRegressor(epsilon=EPSILON, alpha=0.0001, max_iter=1000).fit(
            transform.transform(x[:, selected]), y)
    return ranking, selected, transform, regressor


def predict(rows, selected, transform, regressor):
    x = np.array([row['features'] for row in rows])
    return np.maximum(0, regressor.predict(transform.transform(x[:, selected])))


def estimate(snapshot):
    history = snapshot['history']
    target = snapshot['target']
    holdout_code = history[-1]['shift']
    train_rows = [row for row in history if row['shift'] != holdout_code]
    holdout = [row for row in history if row['shift'] == holdout_code]
    if len(train_rows) < 12 or len(holdout) < 2:
        raise ValueError('至少需要 12 个历史训练小时和 2 个留出小时')
    _, selected, scaler, model = train(train_rows)
    held_prediction = predict(holdout, selected, scaler, model)
    mae = np.mean(np.abs(held_prediction - np.array([row['kg'] for row in holdout])))

    ranking, selected, scaler, model = train(history)
    residual_threshold = EPSILON * float(model.scale_)
    verification_threshold = max(MIN_SPIKE_KG, residual_threshold)
    historical = predict(history, selected, scaler, model)
    fitted = predict(target, selected, scaler, model)
    spikes = []
    for scope, records, baseline in [('history', history, historical), ('target', target, fitted)]:
        for row, value in zip(records, baseline):
            if row['kg'] - value >= verification_threshold:
                spikes.append({'scope': scope, 'shift': row['shift'], 'start': row['start'],
                               'start_read_id': row['start_read_id'], 'end_read_id': row['end_read_id'],
                               'observed_kg': row['kg'], 'baseline_kg': round(float(value), 2)})
    baseline_total = float(fitted.sum())
    measured = sum(row['kg'] for row in target)
    return {
        'algorithm': 'Mutual Information + HuberRegressor', 'random_seed': SEED,
        'parameters': {'top_k': 3, 'n_neighbors': 3, 'epsilon': EPSILON, 'alpha': 0.0001,
                       'max_iter': 1000, 'spike_floor_kg': MIN_SPIKE_KG},
        'sample': {'training': len(train_rows), 'holdout': len(holdout), 'refit': len(history),
                   'holdout_shift': holdout_code, 'historical_shifts': list(dict.fromkeys(row['shift'] for row in history))},
        'metrics': {'holdout_mae_kg': round(float(mae), 2), 'huber_outliers': int(model.outliers_.sum()),
                    'spike_threshold_kg': round(verification_threshold, 2)},
        'features': [{'name': FEATURES[i], 'score': round(score, 4), 'selected': i in selected}
                     for i, score in ranking],
        'profile': [{'start': row['start'], 'observed_kg': row['kg'], 'baseline_kg': round(float(value), 2),
                     'flagged': bool(row['kg'] - value >= verification_threshold)} for row, value in zip(target, fitted)],
        'baseline_kg': round(baseline_total, 2), 'observed_kg': round(measured, 2),
        'excess_kg': round(measured - baseline_total, 2), 'spikes': spikes,
    }
