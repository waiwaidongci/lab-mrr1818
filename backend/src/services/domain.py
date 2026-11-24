"""Build a monthly CNY recurring-contract revenue bridge from effective states."""

import re
from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

DEFAULT_MONTH = '2026-10'
MAX_ROWS = 5000
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

    def add_movements(self, other):
        for field in MOVEMENT_FIELDS:
            setattr(self, field, getattr(self, field) + getattr(other, field))

    def difference(self):
        expected = (self.opening + self.new + self.expansion - self.contraction - self.churn
                    + self.reactivation + self.transfer_in - self.transfer_out)
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


def _read_states(rows):
    if not rows or len(rows) > MAX_ROWS:
        raise ValueError('请导入 1 至 5000 条订阅状态记录，并包含各订阅自首次启用起的变更。')

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


def _detail(month, scope, account, tier, bridge, state=None, action=''):
    return {
        '月份': month,
        '记录类型': scope,
        '账户': account,
        '档位': tier,
        '订阅编号': state.subscription_id if state else '',
        '生效日': state.effective_date.isoformat() if state else '',
        '业务动作': action,
        **{label: getattr(bridge, field) for field, label in BRIDGE_COLUMNS.items()},
        '对账差额（分）': bridge.difference(),
    }


def compute(rows: list[dict], options: dict) -> dict:
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

    details = summary + opening_details + audit
    reconciled = (total.difference() == 0 and total.transfer_in == total.transfer_out
                  and all(row['对账差额（分）'] == 0 for row in details))
    delta = total.closing - total.opening
    headline = f'{month} 期末合同月额 {total.closing:,} 分，较期初净变动 {delta:+,} 分。'
    return {
        'headline': headline,
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
