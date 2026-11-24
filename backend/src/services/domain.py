"""Build a monthly CNY recurring-contract revenue bridge from effective states."""

import re
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

DEFAULT_MONTH = '2026-10'
MAX_ROWS = 5000
# Multiple idempotent supplement batches can form a larger canonical ledger.
MAX_CANONICAL_ROWS = 50_000
# Even a sum across every source event stays below the browser's safe integer limit.
MAX_MRR = 1_000_000_000_000
BRIDGE_COLUMNS = {
    'opening': '期初或变更前（分）',
    'new': '新增（分）',
    'expansion': '扩张（分）',
    'contraction': '收缩（分）',
    'churn': '流失（分）',
    'reactivation': '重新启用（分）',
    'transfer_in': '换档转入（分）',
    'transfer_out': '换档转出（分）',
    'closing': '期末或变更后（分）',
}
ADJUSTMENT_LABEL = '补传调整（分）'
MOVEMENT_FIELDS = tuple(BRIDGE_COLUMNS)[1:-1]


@dataclass(frozen=True)
class SubscriptionState:
    account_id: str
    subscription_id: str
    tier: str
    effective_date: date
    active: bool
    mrr: int


@dataclass
class Bridge:
    opening: int = 0
    new: int = 0
    expansion: int = 0
    contraction: int = 0
    churn: int = 0
    reactivation: int = 0
    transfer_in: int = 0
    transfer_out: int = 0
    closing: int = 0
    # Appended after the original fields to preserve positional compatibility.
    adjustment: int = 0

    def add_movements(self, other):
        for field in MOVEMENT_FIELDS:
            setattr(self, field, getattr(self, field) + getattr(other, field))

    def add_all(self, other):
        self.opening += other.opening
        self.adjustment += other.adjustment
        self.closing += other.closing
        self.add_movements(other)

    def difference(self):
        expected = (self.opening + self.adjustment + self.new + self.expansion - self.contraction
                    - self.churn + self.reactivation + self.transfer_in - self.transfer_out)
        return self.closing - expected


def sample_rows() -> list[dict]:
    tiers = ('基础工位', '标准工位', '专用工位')
    rates = (120_000, 180_000, 260_000)
    rows = []

    def record(subscription_id, tier, effective_date, mrr, status='active'):
        rows.append({
            'account_id': subscription_id.rsplit('-', 1)[0],
            'subscription_id': subscription_id,
            'tier': tier,
            'effective_date': effective_date,
            'status': status,
            'mrr_minor': mrr,
        })

    for account in range(1, 13):
        for booking in (1, 2):
            tier_index = ((account - 1) * 2 + booking - 1) % len(tiers)
            record(f'CW-{account:02d}-{booking:02d}', tiers[tier_index],
                   f'2026-09-{account + 1:02d}', rates[tier_index])

    record('CW-12-02', '专用工位', '2026-09-24', 0, 'inactive')
    record('CW-01-01', '标准工位', '2026-10-01', 180_000)
    record('CW-07-03', '基础工位', '2026-10-04', 120_000)
    record('CW-02-01', '专用工位', '2026-10-08', 230_000)
    record('CW-03-02', '专用工位', '2026-10-12', 0, 'inactive')
    record('CW-12-02', '专用工位', '2026-10-16', 240_000)
    record('CW-04-01', '基础工位', '2026-10-20', 135_000)
    record('CW-05-02', '专用工位', '2026-10-22', 120_000)
    record('CW-06-01', '基础工位', '2026-10-25', 160_000)
    record('CW-08-02', '基础工位', '2026-10-27', 120_000)
    record('CW-13-01', '标准工位', '2026-10-31', 180_000)
    record('CW-02-01', '专用工位', '2026-11-01', 240_000)
    return rows


def _month_bounds(month):
    if isinstance(month, str) and re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', month):
        year, number = map(int, month.split('-'))
        if year:
            return date(year, number, 1), date(year, number, monthrange(year, number)[1])
    raise ValueError('报告月份无效，请按 YYYY-MM 填写有效月份，例如 2026-10。')


def shift_month(month: str, offset: int) -> str:
    first, _ = _month_bounds(month)
    zero_based = (first.month - 1) + offset
    year = first.year + zero_based // 12
    month_number = zero_based % 12 + 1
    return f'{year:04d}-{month_number:02d}'


def previous_month(month: str) -> str:
    return shift_month(month, -1)


def next_month(month: str) -> str:
    return shift_month(month, 1)


def _read_states(rows, max_rows=MAX_ROWS):
    if not rows or len(rows) > max_rows:
        if max_rows == MAX_ROWS:
            raise ValueError('请导入 1 至 5000 条订阅状态记录，并包含各订阅自首次启用起的变更。')
        raise ValueError(f'合并后的订阅状态记录不能超过 {max_rows} 条，请联系管理员分批归档。')

    states = []
    for number, row in enumerate(rows, 1):
        account, subscription, tier = (row.get(key) for key in ('account_id', 'subscription_id', 'tier'))
        if any(not isinstance(value, str) or not value.strip() for value in (account, subscription, tier)):
            raise ValueError(f'第 {number} 条记录缺少账户、订阅编号或档位，请补齐后重新导入。')
        label = f'第 {number} 条记录的订阅 {subscription}'
        effective = row.get('effective_date')
        try:
            if not isinstance(effective, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', effective):
                raise ValueError
            effective_date = date.fromisoformat(effective)
        except ValueError as error:
            raise ValueError(f'{label}生效日无效，请按 YYYY-MM-DD 填写实际日期。') from error
        status = row.get('status')
        if status not in ('active', 'inactive'):
            raise ValueError(f'{label}状态无效，请填写 active 或 inactive。')
        mrr = row.get('mrr_minor')
        if type(mrr) is not int or not 0 <= mrr <= MAX_MRR:
            raise ValueError(f'{label}月额无效，请填写 0 至 {MAX_MRR} 的整数分。')
        if status == 'inactive' and mrr:
            raise ValueError(f'{label}已停订，请将停订后的月额填为 0 分。')
        states.append(SubscriptionState(account, subscription, tier, effective_date, status == 'active', mrr))
    return sorted(states, key=lambda state: (state.effective_date, state.subscription_id))


def _validate_transition(previous, current):
    reference = f'订阅 {current.subscription_id}（{current.effective_date}）'
    if previous is None:
        if not current.active:
            raise ValueError(f'{reference}的首条记录为停订，请补录更早的首次启用记录。')
        return
    if previous.effective_date == current.effective_date:
        raise ValueError(f'{reference}有重复生效记录，请合并为该订阅当天最终生效的一条状态。')
    if previous.account_id != current.account_id:
        raise ValueError(f'{reference}变更了所属账户，请核对账户，换户须停用旧订阅并新建订阅编号。')
    if not current.active and previous.tier != current.tier:
        raise ValueError(f'{reference}的停订档位与此前档位不一致，请使用停订前的档位。')


def _movements(previous, current):
    """Return accounting entries by tier for a dated subscription change."""
    if previous is None:
        return [(current.tier, '首次启用', Bridge(new=current.mrr, closing=current.mrr))]
    if not previous.active:
        if current.active:
            return [(current.tier, '重新启用', Bridge(reactivation=current.mrr, closing=current.mrr))]
        return [(current.tier, '维持停订', Bridge())]
    if not current.active:
        return [(previous.tier, '停订', Bridge(opening=previous.mrr, churn=previous.mrr))]

    delta = current.mrr - previous.mrr
    expansion = max(delta, 0)
    contraction = max(-delta, 0)
    if previous.tier != current.tier:
        transferred = min(previous.mrr, current.mrr)
        return [
            (previous.tier, '换档转出', Bridge(opening=previous.mrr, contraction=contraction,
                                          transfer_out=transferred)),
            (current.tier, '换档转入', Bridge(expansion=expansion, transfer_in=transferred,
                                         closing=current.mrr)),
        ]

    action = '维持生效'
    if expansion:
        action = '月额上调'
    elif contraction:
        action = '月额下调'
    return [(current.tier, action, Bridge(opening=previous.mrr, expansion=expansion,
                                         contraction=contraction, closing=current.mrr))]


def _detail(month, scope, account, tier, bridge, state=None, action='', include_adjustment=False):
    row = {
        '月份': month,
        '记录类型': scope,
        '账户': account,
        '档位': tier,
        '订阅编号': state.subscription_id if state else '',
        '生效日': state.effective_date.isoformat() if state else '',
        '业务动作': action,
        BRIDGE_COLUMNS['opening']: bridge.opening,
    }
    if include_adjustment:
        row[ADJUSTMENT_LABEL] = bridge.adjustment
    for field in MOVEMENT_FIELDS:
        row[BRIDGE_COLUMNS[field]] = getattr(bridge, field)
    row[BRIDGE_COLUMNS['closing']] = bridge.closing
    row['对账差额（分）'] = bridge.difference()
    return row


def _reconciliation_scope(name, bridge):
    return {
        'scope': name,
        'opening': bridge.opening,
        'adjustment': bridge.adjustment,
        'new': bridge.new,
        'expansion': bridge.expansion,
        'contraction': bridge.contraction,
        'churn': bridge.churn,
        'reactivation': bridge.reactivation,
        'transfer_in': bridge.transfer_in,
        'transfer_out': bridge.transfer_out,
        'closing': bridge.closing,
        'difference': bridge.difference(),
    }


def compute(rows: list[dict], options: dict) -> dict:
    options = options or {}
    locked_payload = (options.get('locked_closing_states') or options.get('locked_states')
                      or options.get('previous_closing_states'))
    if locked_payload is not None:
        extra_rows = (options.get('supplement_rows') or options.get('adjustment_rows')
                      or options.get('late_rows') or [])
        return compute_carryforward([*rows, *extra_rows], options, locked_payload, extra_rows)

    month = options.get('report_month', DEFAULT_MONTH)
    first, last = _month_bounds(month)
    states = _read_states(rows)
    latest = {}
    opening = {}
    closing = {}
    changes = []
    for state in states:
        key = state.subscription_id
        previous = latest.get(key)
        _validate_transition(previous, state)
        latest[key] = state
        if state.effective_date < first:
            opening[key] = state
        elif state.effective_date <= last:
            changes.append((previous, state))
        if state.effective_date <= last:
            closing[key] = state

    groups = defaultdict(Bridge)
    opening_details = []
    active_at_open = sorted((state for state in opening.values() if state.active),
                            key=lambda state: (state.account_id, state.tier, state.subscription_id))
    active_at_close = [state for state in closing.values() if state.active]
    for state in active_at_open:
        groups[state.account_id, state.tier].opening += state.mrr
        opening_details.append(_detail(month, '期初订阅', state.account_id, state.tier,
                                       Bridge(opening=state.mrr, closing=state.mrr), state, '上月结转'))
    for state in active_at_close:
        groups[state.account_id, state.tier].closing += state.mrr

    audit = []
    for previous, state in changes:
        for tier, action, movement in _movements(previous, state):
            groups[state.account_id, tier].add_movements(movement)
            audit.append(_detail(month, '生效记录', state.account_id, tier, movement, state, action))

    total = Bridge()
    summary = []
    for (account, tier), bridge in sorted(groups.items()):
        total.opening += bridge.opening
        total.closing += bridge.closing
        total.add_movements(bridge)
        summary.append(_detail(month, '账户档位汇总', account, tier, bridge))

    tier_groups = defaultdict(Bridge)
    for (_, tier), bridge in groups.items():
        tier_groups[tier].add_all(bridge)
    tier_reconciliation = [_reconciliation_scope(tier, bridge)
                           for tier, bridge in sorted(tier_groups.items())]
    total_reconciliation = _reconciliation_scope('全场', total)

    details = summary + opening_details + audit
    reconciled = (total.difference() == 0 and total.transfer_in == total.transfer_out
                  and all(row['对账差额（分）'] == 0 for row in details))
    delta = total.closing - total.opening
    headline = f'{month} 期末合同月额 {total.closing:,} 分，较期初净变动 {delta:+,} 分。'
    return {
        'period': month,
        'month': month,
        'headline': headline,
        'opening': total.opening,
        'closing': total.closing,
        'tier_reconciliation': tier_reconciliation,
        'total_reconciliation': total_reconciliation,
        'metrics': {
            '期初合同月额（分）': total.opening,
            '新增（分）': total.new,
            '扩张（分）': total.expansion,
            '收缩（分）': total.contraction,
            '流失（分）': total.churn,
            '重新启用（分）': total.reactivation,
            '期末合同月额（分）': total.closing,
            '期初活跃订阅数': len(active_at_open),
            '期末活跃订阅数': len(active_at_close),
            '月内生效记录数': len(changes),
            '对账差额（分）': total.difference(),
        },
        'rows': details,
        'notes': [
            '金额以人民币分计，按每条活跃工位订阅的完整合同月额汇总。续订当月不按天折算，发票、押金和一次性费用均不计入。',
            '期初取月首前最后生效的状态，期末含月末当天状态。月内按生效日逐笔归类，月内停订后恢复会同时计流失与重新启用。',
            '同一订阅停订后再生效计重新启用。同账户的其他订阅分别核算，首次启用以订阅编号的完整历史为准。',
            '换档的较低月额在档位间等额转列，价差计入扩张或收缩。换档金额在全场抵消，账户档位汇总需加转入并扣转出。',
            '复核汇总时只合计账户档位汇总行。期初订阅行给出结转所依据的生效日，生效记录行逐订阅列出当月变更。',
            '请确认上游包含首次启用及截至报告月末的全部状态变更。仅凭已导入记录无法判定遗漏。',
        ],
        'chart': {
            'label': '合同月额组成与有符号变动',
            'unit': '分',
            'points': [
                {'label': '期初', 'value': total.opening},
                {'label': '新增', 'value': total.new},
                {'label': '扩张', 'value': total.expansion},
                {'label': '收缩', 'value': -total.contraction},
                {'label': '流失', 'value': -total.churn},
                {'label': '重新启用', 'value': total.reactivation},
                {'label': '期末', 'value': total.closing},
            ],
        },
        'can_publish': reconciled,
    }


def serialize_state(state: SubscriptionState) -> dict:
    return {
        'account_id': state.account_id,
        'subscription_id': state.subscription_id,
        'tier': state.tier,
        'effective_date': state.effective_date.isoformat(),
        'status': 'active' if state.active else 'inactive',
        'mrr_minor': state.mrr,
    }


def closing_states_for_rows(rows: list[dict], month: str,
                            max_rows=MAX_CANONICAL_ROWS) -> dict[str, dict]:
    """Return the validated latest state per subscription at month end."""
    _, last = _month_bounds(month)
    states = _read_states(rows, max_rows)
    latest = {}
    closing = {}
    for state in states:
        _validate_transition(latest.get(state.subscription_id), state)
        latest[state.subscription_id] = state
        if state.effective_date <= last:
            closing[state.subscription_id] = state
    return {key: serialize_state(state) for key, state in sorted(closing.items())}


def _stored_states(payload) -> dict[str, SubscriptionState]:
    if not payload:
        return {}
    if isinstance(payload, dict):
        rows = list(payload.values())
    else:
        rows = list(payload)
    return ({state.subscription_id: state
             for state in _read_states(rows, MAX_CANONICAL_ROWS)} if rows else {})


def _adjustment_entry(old, new):
    """Build self-balancing signed restatement entries by subscription/tier."""
    old_mrr = old.mrr if old is not None and old.active else 0
    new_mrr = new.mrr if new is not None and new.active else 0
    old_tier = old.tier if old is not None and old.active else None
    new_tier = new.tier if new is not None and new.active else None

    if old_mrr == new_mrr and old_tier == new_tier:
        return []

    entries = []
    if old is not None and old.active and (new is None or not new.active or old.tier != new.tier):
        entries.append((old.account_id, old.tier, -old.mrr, old.mrr, 0, old, '冲回已锁档位'))
    if new is not None and new.active and (old is None or not old.active or old.tier != new.tier):
        entries.append((new.account_id, new.tier, new.mrr, 0, new.mrr, new, '按补传状态补列'))
    if old is not None and old.active and new is not None and new.active and old.tier == new.tier:
        delta = new.mrr - old.mrr
        if delta:
            entries.append((old.account_id, old.tier, delta, old.mrr, new.mrr,
                            new, '补传修正月额'))
    return entries


def compute_carryforward(rows: list[dict], options: dict, locked_states_payload,
                         supplement_rows: list[dict] | None = None) -> dict:
    """Compute the first open month from locked closing plus late batches.

    ``opening`` remains the exact locked closing.  Late records are restated in
    a signed adjustment section, after which in-month movements use the
    corrected state as their previous subscription state.
    """
    month = options.get('report_month', DEFAULT_MONTH)
    first, last = _month_bounds(month)
    locked_states = _stored_states(locked_states_payload)
    states = _read_states(rows, MAX_CANONICAL_ROWS)
    supplement_provenance = {
        (row.get('subscription_id'), row.get('effective_date'))
        for row in (supplement_rows or [])
    }
    touched_subscriptions = {key for key, _ in supplement_provenance if key}

    latest = {}
    corrected_open = {}
    corrected_close = {}
    changes = []
    for state in states:
        key = state.subscription_id
        previous = latest.get(key)
        _validate_transition(previous, state)
        latest[key] = state
        if state.effective_date < first:
            corrected_open[key] = state
        elif state.effective_date <= last:
            changes.append((previous, state))
        if state.effective_date <= last:
            corrected_close[key] = state

    groups = defaultdict(Bridge)
    opening_details = []
    locked_active = sorted((state for state in locked_states.values() if state.active),
                           key=lambda state: (state.account_id, state.tier, state.subscription_id))
    for state in locked_active:
        groups[state.account_id, state.tier].opening += state.mrr
        opening_details.append(_detail(month, '期初订阅', state.account_id, state.tier,
                                       Bridge(opening=state.mrr, closing=state.mrr), state,
                                       '上月已锁期末结转', True))

    adjustment_details = []
    adjustment_entries = []
    all_keys = sorted(set(locked_states) | set(corrected_open))
    for key in all_keys:
        old = locked_states.get(key)
        new = corrected_open.get(key)
        entries = _adjustment_entry(old, new)
        if not entries and key in touched_subscriptions:
            reference = new or old
            amount = reference.mrr if reference is not None and reference.active else 0
            entries = [(reference.account_id, reference.tier, 0, amount, amount,
                        reference, '补传后期末无差额')]
        for account, tier, amount, before, after, reference, action in entries:
            movement = Bridge(opening=before, adjustment=amount, closing=after)
            groups[account, tier].adjustment += amount
            adjustment_details.append(_detail(month, '补传调整', account, tier, movement,
                                               reference, action, True))
            adjustment_entries.append({
                'month': month,
                'account_id': account,
                'subscription_id': reference.subscription_id if reference else key,
                'tier': tier,
                'effective_date': reference.effective_date.isoformat() if reference else '',
                'action': action,
                'opening': before,
                'adjustment': amount,
                'closing': after,
                'difference': movement.difference(),
            })

    audit = []
    for previous, state in changes:
        is_supplement = (state.subscription_id, state.effective_date.isoformat()) in supplement_provenance
        for tier, action, movement in _movements(previous, state):
            groups[state.account_id, tier].add_movements(movement)
            audit_action = f'补传{action}' if is_supplement else action
            audit.append(_detail(month, '生效记录', state.account_id, tier, movement,
                                 state, audit_action, True))

    active_at_close = [state for state in corrected_close.values() if state.active]
    for state in active_at_close:
        groups[state.account_id, state.tier].closing += state.mrr

    summary = []
    total = Bridge()
    for (account, tier), bridge in sorted(groups.items()):
        total.add_all(bridge)
        summary.append(_detail(month, '账户档位汇总', account, tier, bridge,
                               include_adjustment=True))

    tier_groups = defaultdict(Bridge)
    for (_, tier), bridge in groups.items():
        tier_groups[tier].add_all(bridge)
    tier_summary = [_detail(month, '档位汇总', '', tier, bridge, include_adjustment=True)
                    for tier, bridge in sorted(tier_groups.items())]
    total_row = _detail(month, '全场汇总', '', '全场', total, include_adjustment=True)

    tier_reconciliation = [_reconciliation_scope(tier, bridge)
                          for tier, bridge in sorted(tier_groups.items())]
    total_reconciliation = _reconciliation_scope('全场', total)

    details = summary + tier_summary + [total_row] + opening_details + adjustment_details + audit
    reconciled = (total.difference() == 0 and total.transfer_in == total.transfer_out
                  and all(row['对账差额（分）'] == 0 for row in details))
    corrected_opening = total.opening + total.adjustment
    delta = total.closing - total.opening
    headline = (f'{month} 承接上月已锁期末 {total.opening:,} 分，补传调整 {total.adjustment:+,} 分，'
                f'修正后期末合同月额 {total.closing:,} 分，较已锁期初净变动 {delta:+,} 分。')
    return {
        'period': month,
        'month': month,
        'carryforward': True,
        'headline': headline,
        'opening': total.opening,
        'locked_closing': total.opening,
        'adjustment_total': total.adjustment,
        'supplement_total': total.adjustment,
        'corrected_opening': corrected_opening,
        'closing': total.closing,
        'adjustments': adjustment_entries,
        'supplement_records': supplement_rows or [],
        'tier_reconciliation': tier_reconciliation,
        'total_reconciliation': total_reconciliation,
        'metrics': {
            '期初合同月额（分）': total.opening,
            '补传调整（分）': total.adjustment,
            '修正后期初合同月额（分）': corrected_opening,
            '新增（分）': total.new,
            '扩张（分）': total.expansion,
            '收缩（分）': total.contraction,
            '流失（分）': total.churn,
            '重新启用（分）': total.reactivation,
            '换档转入（分）': total.transfer_in,
            '换档转出（分）': total.transfer_out,
            '期末合同月额（分）': total.closing,
            '期初活跃订阅数': len(locked_active),
            '修正后期初活跃订阅数': len([s for s in corrected_open.values() if s.active]),
            '期末活跃订阅数': len(active_at_close),
            '补传调整订阅数': len({row['订阅编号'] for row in adjustment_details}),
            '月内生效记录数': len(changes),
            '对账差额（分）': total.difference(),
        },
        'rows': details,
        'notes': [
            f'期初固定等于上一已锁月份结转的期末 {total.opening:,} 分，不随后续补传改写已锁月份。',
            '补传调整按订阅和档位单列：同档位列修正差额，换档在旧档位冲回、新档位补列，因此各档位均对到修正后的期初状态。',
            '月内新增、扩张、收缩、流失、重新启用和换档均以补传修正后的最新状态为前序状态重算，避免重复入账。',
            '分批补传只改变参与合并的批次集合；按订阅和生效日排序后，与一次补齐的计算结果相同。',
            '金额以人民币分计，续订当月不按天折算。换档较低月额转列，价差计入扩张或收缩，全场转列抵消。',
            '复核汇总时只合计账户档位汇总行；档位汇总和全场汇总用于对账，不应与账户档位行重复求和。',
        ],
        'chart': {
            'label': '已锁期初、补传修正与月内变动',
            'unit': '分',
            'points': [
                {'label': '已锁期初', 'value': total.opening},
                {'label': '补传调整', 'value': total.adjustment},
                {'label': '修正期初', 'value': corrected_opening},
                {'label': '新增', 'value': total.new},
                {'label': '扩张', 'value': total.expansion},
                {'label': '收缩', 'value': -total.contraction},
                {'label': '流失', 'value': -total.churn},
                {'label': '重新启用', 'value': total.reactivation},
                {'label': '期末', 'value': total.closing},
            ],
        },
        'can_publish': reconciled,
    }


# Descriptive aliases for callers and tests using the accounting vocabulary.
compute_locked_open_month = compute_carryforward
compute_supplement_bridge = compute_carryforward
compute_adjusted_bridge = compute_carryforward
