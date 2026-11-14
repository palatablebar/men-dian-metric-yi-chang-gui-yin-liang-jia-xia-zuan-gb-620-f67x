import { api } from '../api.js';
import { ENUMS, can, fmtMoney } from '../config.js';
import { drawer, errorBox, errorMessage, esc, field, fmtTime, kv, table, toast } from '../ui.js';

const state = { status: '' };

function runDuration(run) {
  if (!run.started_at || !run.finished_at) return '—';
  const ms = new Date(run.finished_at) - new Date(run.started_at);
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function openRun(view, ctx, run) {
  const checkpoint = run.checkpoint || {};
  const recon = (run.metrics && run.metrics.reconciliation) || {};
  const canResume = can(ctx.user.role, 'run:create') && (run.status === 'failed' || run.status === 'partial');
  const { root } = drawer({
    title: `运行 ${run.run_no}`,
    subtitle: `${esc(ENUMS.trigger[run.trigger] || run.trigger)} · ${esc(ENUMS.runStatus[run.status] || run.status)}`,
    body: `${kv([
      ['状态', esc(ENUMS.runStatus[run.status] || run.status)],
      ['操作人', esc(run.operator_email)],
      ['门店总数', esc(run.store_count)],
      ['完成 / 失败门店', `${(checkpoint.completed_stores || []).length} / ${(checkpoint.failed_stores || []).length}`],
      ['独立异常', esc(run.anomaly_count)],
      ['抑制（同波动去重）', esc((run.metrics && run.metrics.suppressed) || 0)],
      ['影响金额', fmtMoney((run.metrics && run.metrics.impact_total) || 0)],
      ['对账结果', recon.status ? `${esc(recon.status === 'balanced' ? '对平' : '不平')}（${esc(recon.failure_count || 0)} 条不平）` : '—'],
      ['续跑来源', run.recovered_from_id ? `#${esc(run.recovered_from_id)}` : '—'],
      ['开始', fmtTime(run.started_at)],
      ['结束', fmtTime(run.finished_at)],
      ['耗时', runDuration(run)],
    ])}
    <h3 style="margin:14px 0 8px;font-size:0.95rem">指标</h3>
    <pre class="code">${esc(JSON.stringify(run.metrics || {}, null, 2))}</pre>
    <h3 style="margin:14px 0 8px;font-size:0.95rem">运行日志</h3>
    <pre class="code">${esc(run.log_text || '（无日志）')}</pre>`,
    footer: canResume
      ? '<button class="btn accent" data-resume>续跑未完成门店（保留已完成结果与人工复核）</button>'
      : '',
  });
  root.querySelector('[data-resume]')?.addEventListener('click', async () => {
    const button = root.querySelector('[data-resume]');
    button.disabled = true;
    button.textContent = '续跑中…';
    try {
      const continuation = await api.runs.resume(run.id);
      toast(`续跑批次 ${continuation.run_no} 完成：${continuation.anomaly_count} 起独立异常`);
      await runsPage.render(view, ctx);
    } catch (err) {
      toast(errorMessage(err), 'err');
      button.disabled = false;
      button.textContent = '续跑未完成门店（保留已完成结果与人工复核）';
    }
  });
}

export const runsPage = {
  async render(view, ctx) {
    let runs;
    try {
      runs = await api.get('/api/runs', { status: state.status, limit: 50 });
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => runsPage.render(view, ctx));
      return;
    }
    view.innerHTML = `
      <section class="panel">
        <div class="panel-head">
          <h2>运行记录</h2>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="meta">${runs.total} 次</span>
          </div>
        </div>
        <div class="panel-body">
          <div class="filter-bar" id="run-filters">
            ${field({
              label: '状态',
              name: 'status',
              value: state.status,
              options: [
                { value: '', label: '全部' },
                ...Object.entries(ENUMS.runStatus).map(([value, label]) => ({ value, label })),
              ],
            })}
          </div>
        </div>
        ${table({
          rows: runs.items,
          columns: [
            { key: 'run_no', label: '运行号', className: 'mono' },
            {
              key: 'status',
              label: '状态',
              render: (row) => {
                const tone = row.status === 'success' || row.status === 'recovered' ? 'ok' : row.status === 'failed' ? 'danger' : 'warn';
                return `<span class="badge ${tone}">${esc(ENUMS.runStatus[row.status] || row.status)}</span>`;
              },
            },
            { key: 'trigger', label: '触发', render: (row) => esc(ENUMS.trigger[row.trigger] || row.trigger) },
            { key: 'store_count', label: '门店', className: 'num' },
            {
              key: 'progress',
              label: '完成 / 失败',
              className: 'num',
              render: (row) => {
                const cp = row.checkpoint || {};
                return `${(cp.completed_stores || []).length} / ${(cp.failed_stores || []).length}`;
              },
            },
            { key: 'anomaly_count', label: '独立异常', className: 'num' },
            {
              key: 'reconcile',
              label: '对账',
              render: (row) => {
                const recon = (row.metrics && row.metrics.reconciliation) || {};
                if (!recon.status) return '—';
                return recon.status === 'balanced'
                  ? '<span class="badge ok">对平</span>'
                  : `<span class="badge danger">不平 ${esc(recon.failure_count || 0)}</span>`;
              },
            },
            { key: 'started_at', label: '开始时间', render: (row) => fmtTime(row.started_at) },
            {
              key: 'action',
              label: '操作',
              render: (row) =>
                (row.status === 'failed' || row.status === 'partial') && can(ctx.user.role, 'run:create')
                  ? `<button class="btn sm accent" data-resume-id="${row.id}">续跑</button>`
                  : '—',
            },
          ],
        })}
      </section>`;
    const filters = view.querySelector('#run-filters');
    filters?.addEventListener('change', () => {
      state.status = filters.querySelector('[name=status]').value;
      runsPage.render(view, ctx);
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', (event) => {
        if (event.target.closest('[data-resume-id]')) return;
        const run = runs.items.find((item) => String(item.id) === row.dataset.id);
        if (run) openRun(view, ctx, run);
      });
    });
    view.querySelectorAll('[data-resume-id]').forEach((button) => {
      button.addEventListener('click', async (event) => {
        event.stopPropagation();
        const id = Number(button.dataset.resumeId);
        button.disabled = true;
        button.textContent = '续跑中…';
        try {
          const continuation = await api.runs.resume(id);
          toast(`续跑批次 ${continuation.run_no} 完成`);
          await runsPage.render(view, ctx);
        } catch (err) {
          toast(errorMessage(err), 'err');
          button.disabled = false;
          button.textContent = '续跑';
        }
      });
    });
  },
};

export default runsPage;
