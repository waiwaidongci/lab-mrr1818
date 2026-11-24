import {api,download} from '../api/client.js';
import {errorSlot} from '../components/common/layout.js';
import {report} from '../components/common/report.js';
import {table} from '../components/common/table.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc, time} from '../utils/html.js';

function lockBadge(status) {
  if (!status?.locked) return '<p class="hint">月份未锁定；对账平衡后可由复核员锁账。</p>';
  const lock = status.lock;
  return `<p class="notice"><strong>已锁账：${esc(status.month)}</strong> · 锁账人 ${esc(lock?.actor || '')} · ${time(lock?.locked_at)}<br>该月固定按关账时收入桥查看，迟到记录只进入首个未锁月。</p>`;
}

function batchTable(batches) {
  if (!batches?.length) return '<p class="hint">尚无补传批次。</p>';
  return table(batches.map(batch => ({
    批次: `#${batch.id}`,
    入账月份: batch.target_month,
    文件: batch.name,
    记录数: batch.record_count,
    操作人: batch.actor,
    时间: time(batch.created),
    校验: batch.checksum.slice(0, 12),
  })));
}

export function supplementForm(targetMonth, enabled = true) {
  const mayUpload = state.account.role === 'analyst';
  if (!mayUpload) return '<p class="hint">分析员可导入迟到补传；相同内容重传不会重复入账。</p>';
  if (!enabled || !targetMonth) return '<p class="hint">请先由复核员在收入桥页面锁账；锁账后迟到记录会计入首个未锁月。</p>';
  return `<form id="supplement-upload" class="inline-form"><label>迟到补传 CSV<input name="file" type="file" accept=".csv,text/csv" required></label><button type="submit">整批补传至 ${esc(targetMonth)}</button>${errorSlot}</form>`;
}

export function supplementEntry(openMonth) {
  return `<section><h2>锁账后迟到补传</h2><p>已锁月份保持关账快照；迟到的订阅生效记录会整批计入首个未锁月，并在该月按订阅和档位单列补传调整。</p>${supplementForm(openMonth, Boolean(openMonth))}<p class="hint">分批或乱序到达均可，系统按订阅和生效日合并；相同 CSV 或幂等批次重传不会重复入账。</p></section>`;
}

export function accountingPanel(status, options = {}) {
  if (!status) return '';
  const canLockRun = options.run?.result?.can_publish !== false && options.run?.state === 'completed';
  const lockButton = !status.locked && state.account.role === 'reviewer'
    ? (options.run
        ? `<form id="lock-run" class="inline-form"><button type="submit" ${canLockRun ? '' : 'disabled'}>确认锁账 ${esc(status.month)}</button>${errorSlot}</form>`
        : `<form id="lock-period" class="inline-form"><button type="submit">确认锁账 ${esc(status.month)}</button>${errorSlot}</form>`)
    : '';
  const uploadTarget = status.locked ? (status.first_open_month || status.next_month) : status.month;
  const canUpload = status.locked || status.previous_locked;
  const upload = supplementForm(uploadTarget, canUpload);
  const openMonth = status.first_open_month || status.next_month;
  const openLink = status.locked
    ? `<a class="button secondary" href="#3/period-${esc(openMonth)}">查看首个未锁月（${esc(openMonth)}）</a>`
    : (status.previous_locked ? `<a class="button secondary" href="#3/period-${esc(status.month)}">刷新未锁月修正桥</a>` : '');
  const downloadButton = status.bridge_available || status.locked
    ? `<button type="button" id="download-bridge" class="secondary" data-month="${esc(status.month)}">下载收入桥 CSV</button><p id="accounting-download-error" class="error" role="alert"></p>`
    : '';
  return `<section class="accounting-panel"><h2>财务锁账与迟到补传</h2>${lockBadge(status)}<div class="accounting-actions">${lockButton}${openLink}${downloadButton}</div>${upload}<p class="hint">分批或乱序补传会按订阅和生效日合并排序；关账与补传并发时，一个批次只会整批进入同一个未锁月份。</p><h3>${esc(status.month)} 补传批次</h3>${batchTable(status.batches)}</section>`;
}

export function reconciliationTable(result) {
  let rows = [];
  if (result?.tier_reconciliation?.length) {
    rows = result.tier_reconciliation.map(item => ({
      档位: item.scope,
      期初: item.opening,
      补传调整: item.adjustment ?? 0,
      期末: item.closing,
      对账差额: item.difference,
    }));
    if (result.total_reconciliation) {
      rows.push({
        档位: '全场',
        期初: result.total_reconciliation.opening,
        补传调整: result.total_reconciliation.adjustment ?? 0,
        期末: result.total_reconciliation.closing,
        对账差额: result.total_reconciliation.difference,
      });
    }
  } else {
    rows = (result?.rows || [])
      .filter(row => row['记录类型'] === '档位汇总' || row['记录类型'] === '全场汇总')
      .map(row => ({
        档位: row['档位'],
        期初: row['期初或变更前（分）'],
        补传调整: row['补传调整（分）'] ?? 0,
        期末: row['期末或变更后（分）'],
        对账差额: row['对账差额（分）'],
      }));
  }
  if (!rows.length) return '';
  return `<section class="tier-reconciliation"><h3>各档位与全场对账</h3>${table(rows)}</section>`;
}

export function adjustmentTable(result) {
  const rows = (result?.rows || []).filter(row => row['记录类型'] === '补传调整');
  if (!rows.length) return '';
  const columns = ['账户', '档位', '订阅编号', '生效日', '业务动作',
    '期初或变更前（分）', '补传调整（分）', '期末或变更后（分）', '对账差额（分）']
    .map(key => ({key, label: key}));
  return `<section class="adjustment-table"><h3>按订阅和档位的补传调整</h3>${table(rows, columns)}</section>`;
}

export function supplementRecordsTable(result) {
  const rows = result?.supplement_records || [];
  if (!rows.length) return '';
  const profileColumns = state.profile?.columns || [];
  const columns = profileColumns.length
    ? profileColumns.map(field => ({key: field.key, label: field.label}))
    : Object.keys(rows[0]).map(key => ({key, label: key}));
  return `<section class="supplement-records"><h3>补传原始生效记录</h3>${table(rows, columns)}</section>`;
}

export async function periodResults(month) {
  const [status, bridge] = await Promise.all([
    api(`/accounting/status?month=${encodeURIComponent(month)}`),
    api(`/accounting/bridge?month=${encodeURIComponent(month)}`),
  ]);
  return `<section><p><a href="#3">← 返回分析任务</a></p><p class="kicker">${status.locked ? '已锁月份固定快照' : '首个未锁月 · 承接已锁期末'}</p>${accountingPanel(status)}${reconciliationTable(bridge)}${adjustmentTable(bridge)}${supplementRecordsTable(bridge)}${report(bridge)}</section>`;
}

export function bindAccounting(root, refresh) {
  root.querySelector('#download-bridge')?.addEventListener('click', async event => {
    const month = event.currentTarget.dataset.month;
    const error = root.querySelector('#accounting-download-error');
    if (error) error.textContent = '';
    try {
      await download(`/accounting/report.csv?month=${encodeURIComponent(month)}`, `收入桥-${month}.csv`);
    } catch (downloadError) {
      if (error) error.textContent = downloadError.message;
    }
  });

  root.querySelector('#apply-revision-supplement')?.addEventListener('click', async event => {
    const id = event.currentTarget.dataset.id;
    const error = root.querySelector('#apply-supplement-error');
    if (error) error.textContent = '';
    try {
      const saved = await api(`/datasets/${id}/supplement`, {});
      state.status = saved.created
        ? `数据版本 #${id} 已整批作为补传 #${saved.id} 计入 ${saved.target_month}。`
        : `相同补传已存在（批次 #${saved.id}），未重复入账。`;
      location.hash = `3/period-${saved.target_month}`;
    } catch (applyError) {
      if (error) error.textContent = applyError.message;
    }
  });

  bindForm(root, '#lock-run', async form => {
    const id = root.querySelector('#pick-run')?.value || location.hash.split('/')[1];
    await api(`/runs/${id}/lock`, {});
    state.status = '收入桥已锁账；已锁月份将固定显示关账快照。';
  }, refresh);

  bindForm(root, '#lock-period', async form => {
    const month = location.hash.match(/period-(\d{4}-\d{2})/)?.[1];
    if (!month) throw new Error('未找到要锁定的月份。');
    await api(`/periods/${month}/lock`, {});
    state.status = `${month} 已锁账。`;
  }, refresh);

  bindForm(root, '#supplement-upload', async form => {
    const file = form.elements.file.files[0];
    if (!file) throw new Error('请选择补传 CSV。');
    const saved = await api('/supplements', {name: file.name, csv: await file.text()});
    state.status = saved.created
      ? `补传批次 #${saved.id} 已整批计入 ${saved.target_month}。`
      : `相同补传已存在（批次 #${saved.id}），未重复入账。`;
    if (!location.hash.includes(`period-${saved.target_month}`)) {
      location.hash = `3/period-${saved.target_month}`;
    }
  }, refresh);
}
