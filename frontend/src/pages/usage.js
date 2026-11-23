import {ALLOC} from '../constants.js';
import {esc, clock, day, number} from '../utils.js';
import {feedback, heading, notice, report, status} from '../components/ui.js';

const LINE_LABEL = {'LINE-A': '杀菌线（A）', 'LINE-B': '调理线（B）'};

function masterForm() {
  return `<details class="accordion"><summary>录入总表读数</summary><form class="fields" data-path="/master-readings">
    <label>整点时点<input type="datetime-local" name="reading_at" step="3600" required></label>
    <label>累计蒸汽 kg<input name="cumulative_kg" type="number" min="0" step="0.01" required></label>
    <label>原始来源<input name="source_ref" placeholder="总表交接簿页码或照片编号" required></label>
    <button>保存总表读数</button>${feedback}</form></details>`;
}

function allocItem(item, selectedId) {
  const lines = item.lines || {};
  const summary = item.state === 'allocated'
    ? `A ${number(lines['LINE-A'])} / B ${number(lines['LINE-B'])} kg`
    : (item.total_kg === null ? '待数据补齐' : `总耗 ${number(item.total_kg)} kg`);
  return `<a class="roster-item ${item.id === selectedId ? 'selected' : ''}" href="#usage/${item.id}">
    <div class="shift-stamp"><strong>${day(item.window_start)} · ${clock(item.window_start)}–${clock(item.window_end)}</strong>
    <span>${esc(summary)}</span></div>${status(item.state, ALLOC)}</a>`;
}

function modelPane(item) {
  const model = item.result?.model;
  if (!model) return notice('暂无分摊模型', '两线启停与总表读数补齐后，系统按历史同小时工况段拟合分摊。');
  const features = item.result.features || [];
  const coefRow = (label, coefs) => coefs ? `<div><dt>${label}</dt><dd>${coefs.map((value, index) => `${esc(features[index] || '')} ${number(value, 3)}`).join(' · ')}</dd></div>` : '';
  return `<dl class="metrics"><div><dt>历史共用小时</dt><dd>${model.samples}</dd></div>
    ${model.correlation !== undefined ? `<div><dt>两线工况相关系数</dt><dd>${number(model.correlation, 3)}</dd></div>` : ''}
    ${model.intercept !== undefined ? `<div><dt>公共截距 kg/h</dt><dd>${number(model.intercept)}</dd></div>` : ''}
    ${coefRow('杀菌线系数 kg/分钟', model.coef_a)}${coefRow('调理线系数 kg/分钟', model.coef_b)}</dl>`;
}

function allocDetail(item) {
  const lines = item.lines || {};
  const reads = item.reads || [];
  const gauge = (label, value) => `<div><span>${label}</span><strong>${value === undefined || value === null ? '—' : number(value)}<small> kg</small></strong></div>`;
  return `<section class="shift-paper"><div class="sheet-header"><div><p class="eyebrow">共用总表 · 小时分摊</p>
    <h2>${day(item.window_start)} ${clock(item.window_start)}–${clock(item.window_end)}</h2>
    <span>${item.state === 'allocated' ? '两线估算相加等于当小时总表差值' : '该小时尚未给出各线估算'}</span></div>${status(item.state, ALLOC)}</div>
    <div class="gauges">${gauge('当小时总表差值', item.total_kg)}${gauge(`${LINE_LABEL['LINE-A']} 估算`, lines['LINE-A'])}${gauge(`${LINE_LABEL['LINE-B']} 估算`, lines['LINE-B'])}</div>
    ${item.reason ? notice('未分摊原因', item.reason) : ''}
    <div class="capture-columns"><section class="pane">${report('总表抄表', '<span class="subtle">累计表 · kg</span>')}
      ${reads.length ? `<table><thead><tr><th>时点</th><th class="num">累计读数</th><th>原始来源</th></tr></thead><tbody>${reads.map(row =>
        `<tr><td class="mono">${clock(row.reading_at)}</td><td class="num">${number(row.cumulative_kg)}</td><td>${esc(row.source_ref)}</td></tr>`).join('')}</tbody></table>`
      : notice('缺少总表读数', '在用汽分摊页录入整点累计读数后，分摊会自动继续。')}
      </section><section class="pane">${report('分摊模型', '<span class="subtle">历史同小时两线工况拟合</span>')}${modelPane(item)}</section></div></section>`;
}

export function usagePage(list, detail, role) {
  return `${heading('蒸汽分摊', '用汽分摊', '两线共用一块总表时，按历史同小时工况拟合分摊当小时总耗。')}
    <div class="desktop"><aside class="roster">${report('分摊小时', `<span class="subtle">${list.length} 小时</span>`)}
      ${role !== 'viewer' ? masterForm() : ''}
      ${list.map(item => allocItem(item, detail?.id)).join('')}</aside>
    ${detail ? allocDetail(detail) : notice('尚无分摊记录', '两线班次在同一小时重叠并封账后，系统会为该小时建立分摊。')}</div>`;
}
