"use strict";
const $ = (s, el=document) => el.querySelector(s);
const api = async (path, opts={}) => {
  const r = await fetch(path, {headers:{"Content-Type":"application/json"}, ...opts});
  return r.json();
};
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const KINDCN = {scoring:"计分", character:"角色", map:"地图", item:"道具", mechanic:"机制"};
let PATCHES = [];

function patchSelect(id, withSlots=false){
  return `<select id="${id}">` + PATCHES.map(p =>
    `<option value="${p.id}">${p.id} · ${esc(p.title)}（${p.released_on}）</option>`).join("") +
    `</select>` + (withSlots ? ` <select id="${id}-slot">
      <option value="">补丁时点</option><option value="1">同日早更</option>
      <option value="2">同日晚更</option></select>` : "");
}

async function loadPatches(){
  const d = await api("/api/patches");
  PATCHES = d.patches;
}

/* ---------------- Tab routing ---------------- */
const tabs = ["search","explain","examples","packages","errata","index"];
document.querySelectorAll("#tabs button").forEach(b => b.onclick = () => {
  document.querySelectorAll("#tabs button").forEach(x => x.classList.toggle("active", x===b));
  tabs.forEach(t => $("#tab-"+t).hidden = (t !== b.dataset.tab));
  if (b.dataset.tab === "examples") renderExamples();
  if (b.dataset.tab === "packages") renderPackages();
  if (b.dataset.tab === "errata") renderErrata();
  if (b.dataset.tab === "index") renderIndex();
});

function diagHtml(diags){
  if (!diags || !diags.length) return "";
  return diags.map(d => `<div class="diag ${d.severity||"error"}">
    <b>${esc(d.code||"DIAGNOSTIC")}</b> — ${esc(d.message||"")}
    ${d.rule_id ? `<div class="muted">规则: ${esc(d.rule_id)}</div>`:""}
    ${d.cycle ? `<pre>${esc(JSON.stringify(d.cycle))}</pre>`:""}
  </div>`).join("");
}

/* ---------------- 1. Search ---------------- */
window.addEventListener("DOMContentLoaded", async () => {
  await loadPatches();
  initExplainTab();
  $("#tab-search").innerHTML = `
    <div class="card">
      <div class="row">
        <input type="text" id="q" placeholder="搜索角色 / 地图 / 道具 / 计分，例如：游侠、冰原、kill、限时">
        <select id="kind">
          <option value="">全部类型</option>
          <option value="character">角色</option><option value="map">地图</option>
          <option value="item">道具</option><option value="scoring">计分</option>
        </select>
        ${patchSelect("s-anchor")}
        <label class="muted"><input type="checkbox" id="history"> 历史过滤（默认只搜当前版）</label>
        <button class="primary" id="go">检索</button>
      </div>
      <div class="muted" id="s-meta"></div>
    </div>
    <div id="s-results"></div>`;
  $("#go").onclick = doSearch;
  $("#q").addEventListener("keydown", e => e.key==="Enter" && doSearch());
  doSearch();
});

async function doSearch(){
  const q = $("#q").value.trim();
  const kind = $("#kind").value;
  const history = $("#history").checked;
  const anchor = history ? $("#s-anchor").value : "";
  const params = new URLSearchParams({q, history: history?"1":"0"});
  if (kind) params.set("kind", kind);
  if (anchor) params.set("anchor_patch", anchor);
  const d = await api("/api/search?"+params.toString());
  $("#s-meta").textContent =
    `命中 ${d.results.length} 条 · ${d.current_only?"仅当前版（默认）":"历史时点 "+(d.anchor_patch||"")} · 索引状态: ${d.index_status}`;
  $("#s-results").innerHTML = d.results.map(r => `
    <div class="card">
      <div>
        <span class="tag kind-${r.kind}">${KINDCN[r.kind]||r.kind}</span>
        <b>${esc(r.name)}</b> <code>${esc(r.rule_id)}</code>
        <span class="pill">版本 ${esc(r.version_label)} · ${esc(r.status)}</span>
        ${r.effective_at_anchor?'<span class="tag ok">该时点生效</span>':'<span class="tag">非该时点</span>'}
      </div>
      <div class="muted">${esc(r.summary)} · 区间 ${esc(r.interval[0]||"")} → ${esc(r.interval[1]||"当前")}</div>
      <div style="margin-top:6px">${esc(r.body)}</div>
    </div>`).join("") || `<div class="card muted">无命中。试试“击败 / 冰 / 护符 / 连击”。</div>`;
}

/* ---------------- 2. Explain ---------------- */
const DEFAULT_SITUATION = {
  character:"char_ranger",
  stats:{kills:3, combo:8},
  match:{duration_sec:95},
  map:{terrain:"ice"},
  events:{terrain_used:true},
  items:{ice_charm:false}
};
function initExplainTab(){
$("#tab-explain").innerHTML = `
  <div class="grid2">
    <div class="card">
      <div class="row" style="margin-bottom:8px">
        <b>局面输入（JSON）</b> ${patchSelect("e-anchor", true)}
      </div>
      <textarea id="situation">${esc(JSON.stringify(DEFAULT_SITUATION,null,2))}</textarea>
      <div class="row" style="margin-top:8px">
        预算 <input type="text" id="budget" value="5000" style="min-width:90px">
        <input type="text" id="roots" placeholder="额外根规则，逗号分隔（可选，如 diverge_rule）" style="min-width:260px">
        <button class="primary" id="explain-go">解释该局面</button>
      </div>
    </div>
    <div id="explain-out" class="card muted">选择补丁与局面后点击“解释该局面”。</div>
  </div>`;
$("#explain-go").onclick = async () => {
  let situation;
  try { situation = JSON.parse($("#situation").value); }
  catch(e){ $("#explain-out").innerHTML = `<div class="diag error">JSON 解析失败：${esc(e.message)}</div>`; return; }
  const body = {situation, anchor_patch:$("#e-anchor").value,
    day_slot: parseInt($("#e-anchor-slot").value||"0")||null,
    budget: parseInt($("#budget").value||"5000",10),
    roots: $("#roots").value.split(",").map(s=>s.trim()).filter(Boolean)};
  $("#explain-out").innerHTML = `<div class="muted">求值中…</div>`;
  const d = await api("/api/explain", {method:"POST", body:JSON.stringify(body)});
  renderExplanation(d, "explain-out", true);
};
}

function renderExplanation(d, elId, showPackage){
  const el = $("#"+elId);
  const scoreHtml = d.score===null || d.score===undefined
    ? `<div class="bigscore" style="color:var(--err)">无法求解</div>`
    : `<div class="bigscore">${d.score} 分</div>`;
  el.innerHTML = `
    <div class="row" style="justify-content:space-between">
      <div>${scoreHtml}
        <div class="muted">锚点 ${esc(d.anchor_patch)} 坐标 ${d.anchor_coord}
          · 包状态 <span class="tag ${d.package_status==='ok'?'ok':'bad'}">${d.package_status}</span>
          ${d.cache_hit?'<span class="tag ok">缓存命中</span>':'<span class="tag warn">新建包</span>'}
          · ${d.steps_used}/${d.budget} 步</div>
      </div>
      <button class="ghost" id="export-${elId}">导出报告</button>
    </div>
    <h4>逐步解释：分数来源</h4>
    ${(d.step_explanation||[]).map(s=>`<div class="step ${s.action}">${esc(s.text)}</div>`).join("")}
    ${diagHtml(d.diagnostics)}
    ${diagHtml(d.package_diagnostics)}
    ${(d.errata_notices||[]).length ? `<h4>勘误提示（不改变本冻结局面的分数）</h4>` +
      d.errata_notices.map(e=>`<div class="diag warning">${esc(e.patch_id)} 勘误：${esc(e.rule_id)}
        ${esc(e.bad_label)} → ${esc(e.fixed_label)}<br>${esc(e.note)}</div>`).join("") : ""}
    ${showPackage ? `<details style="margin-top:10px"><summary class="muted">冻结规则闭包（${d.frozen_summary?.rule_count||0} 条规则）</summary>
      <pre>${esc(JSON.stringify(d.roots))} 根规则；包ID ${esc(d.package_id)}</pre></details>` : ""}`;
  const btn = $("#export-"+elId);
  if (btn) btn.onclick = async () => {
    // include the situation input whenever this report is exported from the
    // explain tab; the report then carries situation_input + score breakdown
    let situation = null;
    const ta = $("#situation");
    if (ta) {
      try { situation = JSON.parse(ta.value); }
      catch(e){ /* fall back to package-only report */ }
    }
    let rep;
    if (situation) {
      rep = await api("/api/report", {method:"POST",
        body: JSON.stringify({package_id:d.package_id, situation})});
    } else {
      rep = await api(`/api/report?package_id=${encodeURIComponent(d.package_id)}`);
    }
    const blob = new Blob([JSON.stringify(rep,null,2)], {type:"application/json"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `rule-report-${d.package_id.slice(0,8)}.json`;
    a.click();
  };
}

/* ---------------- 3. Examples ---------------- */
async function renderExamples(){
  const d = await api("/api/examples");
  $("#tab-examples").innerHTML = `
    <div class="card muted">已发布示例的局面与整套规则闭包冻结保存；回放不会被后续补丁/勘误改写。</div>
    <div class="card"><table><thead><tr><th>示例</th><th>发布补丁</th><th>锚点</th><th>冻结包</th>
    <th>期望分</th><th></th></tr></thead><tbody>
    ${d.examples.map(e=>`<tr>
      <td><b>${esc(e.title)}</b><br><code>${esc(e.id)}</code></td>
      <td>${esc(e.published_patch)}</td><td>${esc(e.anchor_patch)}</td>
      <td>${e.frozen_package_id?'<span class="tag ok">已冻结</span>':'<span class="tag warn">待冻结</span>'}</td>
      <td>${e.expected_score??"—"}</td>
      <td><button class="ghost" data-replay="${e.id}">冻结回放</button>
          <button class="ghost" data-advisory="${e.id}">现行规则参考</button></td>
    </tr>`).join("")}
    </tbody></table></div>
    <div id="ex-out"></div>`;
  document.querySelectorAll("[data-replay]").forEach(b => b.onclick = async () => {
    const d2 = await api(`/api/example/replay?id=${encodeURIComponent(b.dataset.replay)}`);
    renderReplay(d2);
  });
  document.querySelectorAll("[data-advisory]").forEach(b => b.onclick = async () => {
    const d2 = await api(`/api/example/advisory?id=${encodeURIComponent(b.dataset.advisory)}`);
    $("#ex-out").innerHTML = `<div class="card"><h4>现行/勘误规则参考分（advisory）</h4>
      <div class="bigscore">${d2.score??"无法求解"}</div>
      <div class="muted">${esc(d2.note)}</div>
      ${(d2.step_explanation||[]).map(s=>`<div class="step ${s.action}">${esc(s.text)}</div>`).join("")}
    </div>`;
  });
}
function renderReplay(d){
  $("#ex-out").innerHTML = `<div class="card">
    <h4>${esc(d.title||d.example_id)} <span class="pill">期望 ${d.expected_score??"—"}</span></h4>
    <div class="bigscore">${d.score??"无法求解"}</div>
    <div class="muted">${esc(d.replay_note||"")}</div>
    ${(d.step_explanation||[]).map(s=>`<div class="step ${s.action}">${esc(s.text)}</div>`).join("")}
    ${diagHtml(d.diagnostics)}
    ${(d.errata_notices||[]).map(e=>`<div class="diag warning">勘误关系：${esc(e.rule_id)}
      ${esc(e.bad_label)} → ${esc(e.fixed_label)}（${esc(e.note)}）</div>`).join("")}
  </div>`;
}

/* ---------------- 4. Packages ---------------- */
async function renderPackages(){
  const d = await api("/api/packages");
  const rows = d.packages;
  const pre = rows.filter(r=>r.kind==="precompiled"), q = rows.filter(r=>r.kind==="query");
  $("#tab-packages").innerHTML = `
    <div class="card muted">预编译包按补丁提前生成（单次索引命中）；按查询组合用 (锚点坐标, 根集合) 作缓存身份，
      覆盖相同规则时与预编译包的内容哈希一致。</div>
    <div class="card">
      <h4>预编译包（每补丁一个）</h4>
      <table><thead><tr><th>锚点</th><th>状态</th><th>规则数</th><th>边/定点</th><th>包ID</th></tr></thead><tbody>
      ${pre.map(r=>`<tr class="clickable" data-pkg="${r.package_id}">
        <td>${esc(r.anchor_patch)} <span class="muted">@${r.anchor_coord}</span></td>
        <td><span class="tag ${r.status==='ok'?'ok':'bad'}">${r.status}</span></td>
        <td>${r.stats.rule_count}</td><td>${r.stats.edge_count} / ${r.stats.bounded_fixed_points}</td>
        <td><code>${esc(r.package_id.slice(0,12))}…</code></td></tr>`).join("")}
      </tbody></table>
      <h4>查询组合包</h4>
      <table><thead><tr><th>锚点</th><th>根</th><th>状态</th><th>规则数</th><th>包ID</th></tr></thead><tbody>
      ${q.map(r=>`<tr class="clickable" data-pkg="${r.package_id}">
        <td>${esc(r.anchor_patch)} @${r.anchor_coord}</td>
        <td>${r.roots.map(esc).join(", ")}</td>
        <td><span class="tag ${r.status==='ok'?'ok':'bad'}">${r.status}</span></td>
        <td>${r.stats.rule_count}</td>
        <td><code>${esc(r.package_id.slice(0,12))}…</code></td></tr>`).join("")||
        '<tr><td colspan="5" class="muted">暂无（在“局面解释”中求值后生成）</td></tr>'}
      </tbody></table>
    </div>
    <div id="pkg-out"></div>`;
  document.querySelectorAll("[data-pkg]").forEach(tr => tr.onclick = async () => {
    const p = await api(`/api/package?id=${encodeURIComponent(tr.dataset.pkg)}`);
    $("#pkg-out").innerHTML = `<div class="card">
      <h4>规则包详情</h4>
      <div class="muted">${p.kind} · ${p.anchor_patch} @${p.anchor_coord} · ${p.package_id}</div>
      ${diagHtml(p.diagnostics)}
      <table><thead><tr><th>规则</th><th>类型</th><th>版本</th><th>生效区间(坐标)</th><th>说明</th></tr></thead><tbody>
      ${Object.values(p.frozen).map(v=>`<tr>
        <td><code>${esc(v.rule_id)}</code></td>
        <td><span class="tag kind-${v.kind}">${KINDCN[v.kind]||v.kind}</span></td>
        <td>${esc(v.version_label)}</td>
        <td>[${v.effective_interval_coord[0]}, ${v.effective_interval_coord[1]??"∞"}]</td>
        <td>${esc(v.body)}</td></tr>`).join("")}
      </tbody></table></div>`;
  });
}

/* ---------------- 5. Errata ---------------- */
async function renderErrata(){
  const [e, ov] = await Promise.all([api("/api/errata"), api("/api/diagnostics/overlaps")]);
  $("#tab-errata").innerHTML = `
    <div class="card">
      <h4>勘误与修正关系（不重写已发布示例）</h4>
      ${e.errata.map(x=>`<div class="diag warning"><b>${esc(x.patch_id)}</b> · 规则
        <code>${esc(x.rule_id)}</code>：错误版本 ${esc(x.bad_label)}
        → 修正版本 <b>${esc(x.fixed_label)}</b><div>${esc(x.note)}</div></div>`).join("")}
    </div>
    <div class="card">
      <h4>生效区间重叠审计（关系库完整性）</h4>
      ${ov.overlaps.length ? ov.overlaps.map(x=>`<div class="diag error">${esc(x.message)}
        <pre>${esc(JSON.stringify(x.versions,null,2))}</pre></div>`).join("")
        : '<div class="muted">当前种子数据无重叠（验收测试会动态制造重叠并验证诊断）。</div>'}
    </div>`;
}

/* ---------------- 6. Index ---------------- */
async function renderIndex(){
  const d = await api("/api/index/status");
  const bar = d.total ? Math.round((d.done/d.total)*100) : 0;
  $("#tab-index").innerHTML = `
    <div class="card">
      <h4>检索索引：可中断 / 可恢复的增量构建</h4>
      <p class="muted">状态：<b id="ix-status">${d.status}</b> · ${d.done}/${d.total||"?"}
        · 第 ${d.generation} 代 ${d.message? "· "+esc(d.message):""}</p>
      <div class="progress"><div id="ix-bar" style="width:${bar}%"></div></div>
      <div class="row" style="margin-top:10px">
        <button class="primary" id="ix-start">全新构建</button>
        <button class="ghost" id="ix-resume">恢复构建</button>
        <button class="ghost" id="ix-batch">推进 3 条</button>
        <button class="ghost" id="ix-interrupt">模拟中断</button>
      </div>
      <p class="muted">中断后检索仍可用（已提交批次 + LIKE 回退）；恢复从中断处继续，不重复索引。</p>
    </div>`;
  const refresh = async () => {
    const s = await api("/api/index/status");
    const b = s.total ? Math.round((s.done/s.total)*100):0;
    $("#ix-status").textContent = s.status + (s.message? " · "+s.message:"");
    $("#ix-bar").style.width = b+"%";
  };
  $("#ix-start").onclick = async () => { await api("/api/index/start",{method:"POST",body:JSON.stringify({resume:false})}); refresh(); };
  $("#ix-resume").onclick = async () => { await api("/api/index/start",{method:"POST",body:JSON.stringify({resume:true})});
    let s; do { s = await api("/api/index/batch",{method:"POST",body:JSON.stringify({limit:5})}); } while(s.status==="building"); refresh(); };
  $("#ix-batch").onclick = async () => { await api("/api/index/batch",{method:"POST",body:JSON.stringify({limit:3})}); refresh(); };
  $("#ix-interrupt").onclick = async () => { await api("/api/index/interrupt",{method:"POST",body:"{}"}); refresh(); };
}
