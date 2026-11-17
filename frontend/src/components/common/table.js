import {fmt,esc} from '../../utils/format.js';
// 单元格默认经 fmt 转义；用 cell.raw() 显式放行调用方已转义的原生控件。
const RAW=Symbol('raw');
export const raw=html=>({[RAW]:String(html)});
const cell=v=>v&&typeof v==='object'&&v[RAW]!==undefined?v[RAW]:fmt(v);
export function table(rows,cols){if(!rows.length)return '<div class="empty">尚无记录。先添加底稿，再新建核算任务；任务完成后可复核并设置交付。</div>';cols=cols||Object.keys(rows[0]);return '<div class="table-wrap"><table><thead><tr>'+cols.map(k=>'<th>'+esc(k)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+cols.map(k=>'<td>'+cell(r[k])+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>'}
