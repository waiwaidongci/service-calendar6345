import {heading} from '../components/common/header.js';
import {table} from '../components/common/table.js';
import {esc,time} from '../utils/format.js';

const statusLabel={building:'基线装载中',catching:'追赶变更中',active:'对外生效',superseded:'已归档',failed:'建账失败'};

function progress(p){
  if(!p)return '';
  const total=Math.max(1,p.base_total),done=Math.min(p.base_done,total);
  const pct=p.base_complete?100:Math.round(done/total*100);
  return `<div class="pivot-progress"><div class="bar"><i style="width:${pct}%"></i></div><p>${p.base_complete?'基线已完成':`基线装载 ${done}/${total} 条`} · ${pct}% · 状态 ${esc(statusLabel[p.status]||p.status)}</p></div>`;
}

export function pivot(c,d){
  const p=d.pivot||{};
  const a=p.active;
  const b=p.building;
  return heading('INCREMENTAL PIVOT / 06','固定口径增量透视','底稿增删改经变更日志自动计入：先撤旧贡献再计新贡献；单条更正不扫描全库。页面只读取同一水位的完整结果。')
  +`<div class="pivot-meta">
     <div><small>接收水位</small><strong>${esc(p.receive_seq??'—')}</strong></div>
     <div><small>页面水位</small><strong>${a?esc(a.watermark):'—'}</strong></div>
     <div><small>滞后变更</small><strong class="${a&&a.lag>0?'attention':''}">${a?esc(a.lag):'—'}</strong></div>
     <div><small>生效口径</small><strong>${a?esc(a.policy_version):'建账中'}</strong></div>
     <div><small>页面水位时间</small><strong class="small">${a&&a.watermark_at?esc(time(a.watermark_at)):'—'}</strong></div>
    </div>
    <p class="pivot-reason ${a&&a.lag>0?'attention':''}">${esc(p.reason||'正在读取进度…')}</p>
    ${b?`<section class="pivot-build"><h3>新口径建账 · ${esc(b.policy_version)}</h3>${progress(b)}<p>${b.base_complete?`正在追赶变更 ${b.backlog??'?'} 条，追平后原子切换；切换前下方仍是旧口径完整结果。`:'建账期间新增/修改/删除照常接收，不阻塞写入。'}</p>${b.error?`<p class="error">建账失败：${esc(b.error)}（旧结果保留）</p>`:''}</section>`:''}
    ${a?`<h2>生效透视 · ${esc(a.policy_version)}</h2><div class="metrics">${Object.entries(a.summary).map(([k,v])=>`<div><small>${esc(k)}</small><strong>${esc(typeof v==='number'?v.toLocaleString('zh-CN',{maximumFractionDigits:2}):v)}</strong></div>`).join('')}</div><div class="table-wrap"><table><thead><tr>${Object.keys(a.rows[0]||{'月份':0}).map(k=>`<th>${esc(k)}</th>`).join('')}</tr></thead><tbody>${a.rows.map(r=>`<tr><td>${esc(r['月份'])}</td><td>${esc(r['小组'])}</td><td>${esc(r['优先级'])}</td><td>${esc(r['样本数'])}</td><td>${esc(Number(r['业务分钟合计']).toLocaleString('zh-CN',{maximumFractionDigits:2}))}</td><td>${esc(Number(r['平均业务分钟']).toLocaleString('zh-CN',{maximumFractionDigits:2}))}</td><td>${esc(r['超时数'])}</td><td>${(r['超时率']*100).toFixed(1)}%</td></tr>`).join('')}</tbody></table></div>`:'<div class="empty">首次建账尚未完成，完成后此处显示固定口径透视。</div>'}
    ${p.issues&&p.issues.length?`<h2>未计入问题清单</h2><p class="warning">这些底稿因口径不完整（如优先级缺少SLA阈值、时间格式不合法）无法计入，但不阻塞水位推进；底稿更正或新口径建账后会自动恢复。</p>${table(p.issues.map(x=>({底稿编号:x.source_id,事件序号:x.seq,原因:x.reason,时间:time(x.at)})))}`:''}
    <h2>处理进程</h2>
    ${p.workers&&p.workers.length?table(p.workers.map(w=>({进程:w.name,心跳距今秒:w.age,状态:w.alive?'正常':'无心跳'}))):'<div class="empty">尚未收到处理进程心跳。增量透视将在处理进程启动后继续。</div>'}
    ${(p.failed&&p.failed.length)?`<h2>建账失败记录</h2><p class="warning">重建失败不会替换生效结果；以上快照保留错误供排查，修正后可在下方按同一口径重建。</p>${table(p.failed.map(x=>({编号:x.id,口径:x.policy_version,错误:x.error})))}`:''}
    <h2>快照记录</h2><p class="hint">切换与重建都会新增快照；旧快照保留原结果，不会被后续底稿改写。</p>
    ${table((p.snapshots||[]).map(s=>({编号:s.id,口径:s.policy_version,状态:statusLabel[s.status]||s.status,水位:s.watermark,基线:`${s.base_done}/${s.base_total}`,创建:s.created?time(s.created):'',生效:s.activated?time(s.activated):'',说明:s.error||s.last_error||''})))}
    <div class="pivot-actions"><button id="pivot-rebuild" class="ghost">按当前口径重建</button><span class="hint">重建期间继续接收改动；追平接收水位后原子切换，失败则旧结果继续服务。</span></div>`;
}
