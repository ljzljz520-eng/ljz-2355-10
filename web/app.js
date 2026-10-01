"use strict";
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json();
  if (!r.ok) {
    const e = new Error(data.message || ("HTTP " + r.status));
    e.payload = data;
    throw e;
  }
  return data;
}
const post = (path, body) => api(path, {
  method: "POST", headers: {"Content-Type": "application/json"},
  body: JSON.stringify(body || {})});

function toast(msg, ms) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.add("hidden"), ms || 2600);
}
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt = v => (typeof v === "number" ? String(Math.round(v * 1e6) / 1e6) : esc(v));

// ---------- Tabs ----------
$$(".tabs button").forEach(b => b.addEventListener("click", () => {
  $$(".tabs button").forEach(x => x.classList.remove("active"));
  $$(".tab").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  $("#tab-" + b.dataset.tab).classList.add("active");
}));

// ---------- Meta ----------
(async () => {
  const m = await api("/api/meta");
  $("#meta").textContent =
    `当前时间(基准) ${m.now} ｜ 当前版补丁 ${m.current_package} ｜ 编译缓存条目 ${m.cache.entries}`;
})();

// ---------- 搜索 ----------
$("#hist").addEventListener("change", e => $("#asof").disabled = !e.target.checked);
async function doSearch() {
  const p = new URLSearchParams();
  p.set("q", $("#q").value.trim());
  if ($("#dtype").value) p.set("type", $("#dtype").value);
  if ($("#hist").checked && $("#asof").value.trim()) p.set("as_of", $("#asof").value.trim());
  if ($("#allHist").checked) p.set("all", "1");
  const data = await api("/api/search?" + p.toString());
  const box = $("#searchResults");
  if (!data.results.length) { box.innerHTML = "<p class='muted'>无匹配结果</p>"; return; }
  box.innerHTML = data.results.map(r => `
    <div class="doc">
      <div class="t"><span class="badge ${r.doc_type}">${labelType(r.doc_type)}</span>${esc(r.title)}
        ${r.historical ? "<span class='badge hist'>历史</span>" : ""}</div>
      <div class="s">${esc(r.snippet)}</div>
      <div class="s kv">
        ${r.package_id ? `来源补丁 <b>${esc(r.package_id)}</b> ｜ ` : ""}
        生效 <b>${esc(r.valid_from || "")}</b>${r.valid_to ? " → " + esc(r.valid_to) : " → 开放"}
        ｜ <code>${esc(r.tags)}</code>
      </div>
    </div>`).join("");
}
function labelType(t) {
  return {rule: "规则", package: "补丁", errata: "勘误", example: "示例"}[t] || t;
}
$("#btnSearch").addEventListener("click", doSearch);
$("#q").addEventListener("keydown", e => e.key === "Enter" && doSearch());

// ---------- 比赛示例回放 ----------
async function loadExamples() {
  const data = await api("/api/examples");
  $("#exampleSel").innerHTML = data.examples.map(x =>
    `<option value="${x.example_id}">${esc(x.title)}（${x.played_at}）</option>`).join("");
  window._examples = data.examples;
}
$("#btnReplay").addEventListener("click", async () => {
  const id = $("#exampleSel").value;
  try {
    const r = await api("/api/examples/" + id);
    renderReplay(r);
  } catch (e) { renderDiag($("#replayMeta"), e.payload); }
});
function renderReplay(r) {
  const totalKey = Object.keys(r.expected).filter(k => k.startsWith("total")).pop();
  $("#replayMeta").innerHTML = `
    <h3>${esc(r.title)}</h3>
    <div class="kv">比赛时间 <b>${esc(r.played_at)}</b> ｜ 备注 ${esc(r.note)}</div>
    <div class="kv">局面输入 <code>${esc(JSON.stringify(r.inputs))}</code></div>
    <div class="kv">已发布结果 <b>${fmt(r.expected[totalKey])}</b> ｜ 冻结闭包重算 <b>${fmt(r.recomputed[totalKey])}</b>
      ${r.matches_published ? "<span class='ok'> ✓ 与已发布一致</span>"
                            : "<span class='bad'> ✗ 不一致</span>"}</div>`;
  $("#freezeBanner").classList.remove("hidden", "fix");
  $("#freezeBanner").innerHTML =
    `🧊 历史回放冻结整套规则闭包 <code>${esc(r.frozen_hash)}</code>：角色、地图、道具、` +
    `击杀/计分公式与冲突策略都取自同一冻结快照，<strong>不会</strong>出现“旧角色数值 + 新计分公式”的错配。`;
  $("#finalScore").textContent = JSON.stringify(r.recomputed, null, 2);
  renderSteps($("#steps"), r.trace.steps);
  // 勘误修正关系（不重写已发布结果）
  if (r.corrections && r.corrections.length) {
    $("#corrections").innerHTML = r.corrections.map(c => `
      <div class="corr">📝 勘误 <b>${esc(c.errata_id)}</b>（修正关系，独立标注，不改写原结果）<br/>
        修正闭包 <code>${esc(c.corrected_hash)}</code> 重算总分
        <b>${fmt(c.recomputed[totalKey])}</b>（原已发布 ${fmt(r.expected[totalKey])}）<br/>
        <span class="muted">${esc(c.note)}</span></div>`).join("");
  } else $("#corrections").innerHTML = "";
  window._lastReplay = r;
}
$("#btnReportJson").addEventListener("click", () => exportReport("json"));
$("#btnReportMd").addEventListener("click", () => exportReport("md"));
async function exportReport(fmt) {
  const r = window._lastReplay;
  if (!r) return toast("请先回放一个比赛示例");
  const rep = await post("/api/report", {
    snapshot_hash: r.frozen_hash, inputs: r.inputs, format: fmt});
  const blob = new Blob([fmt === "md" ? rep : JSON.stringify(rep, null, 2)],
    {type: fmt === "md" ? "text/markdown" : "application/json"});
  download(`rule-report-${r.example_id}.${fmt === "md" ? "md" : "json"}`, blob);
}
function download(name, blob) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name; a.click();
  URL.revokeObjectURL(a.href);
}

// ---------- 逐步追踪渲染 ----------
function renderSteps(root, steps) {
  root.innerHTML = steps.map((s, i) => {
    const cls = ["step", s.kind, s.applicable ? "" : "off"].join(" ");
    const badge = {rule: "规则", aggregator: "槽位汇总", fixpoint: "有界环"}[s.kind];
    return `<div class="${cls}" data-i="${i}">
      <div class="head">
        <span class="badge">${badge}</span>
        <span>${esc(s.title)}</span>
        <span class="rid">${esc(s.rule_id)}</span>
        <span class="val">${s.applicable ? "= " + fmt(s.value) : "未生效（0）"}</span>
      </div>
      <div class="body">
        <div>公式：<code>${esc(s.formula)}</code></div>
        ${Object.keys(s.inputs || {}).length ?
          `<div class="kv">局面输入：<code>${esc(JSON.stringify(s.inputs))}</code></div>` : ""}
        ${Object.keys(s.refs || {}).length ?
          `<div class="kv">引用取值：<code>${esc(JSON.stringify(s.refs))}</code></div>` : ""}
        ${s.note ? `<div class="kv">说明：${esc(s.note)}</div>` : ""}
        ${s.iterations ? `<div class="kv">有界同步迭代：<b>${s.iterations}</b> 轮收敛
          <div class="rounds">${(s.rounds||[]).map((r,k)=>
            `r${k+1}: ${esc(JSON.stringify(r))}`).join("<br/>")}</div></div>` : ""}
      </div>
    </div>`;
  }).join("");
  root.querySelectorAll(".step .head").forEach(h => h.addEventListener("click", () =>
    h.parentElement.classList.toggle("open")));
}

// ---------- 自定义局面 ----------
const FILLS = {
  magnet: {character: "nova", map: "neon", item: "magnet", kills: 1, ranked_kills: 1},
  shield: {character: "nova", map: "yard", item: "shield", kills: 2, ranked_kills: 1},
  budget: {n: 100000, character: "", map: "", item: ""}
};
$("#cInputs").value = JSON.stringify(FILLS.shield, null, 2);
$$("[data-fill]").forEach(b => b.addEventListener("click", () => {
  $("#cInputs").value = JSON.stringify(FILLS[b.dataset.fill], null, 2);
  if (b.dataset.fill === "budget") {
    $("#cPkg").value = "test-heavy";
    $("#cAsof").value = ""; $("#cHash").value = "";
  }
}));
$("#btnExplain").addEventListener("click", async () => {
  let inputs;
  try { inputs = JSON.parse($("#cInputs").value); }
  catch { return renderDiag($("#customDiag"), {code:"BAD_JSON",message:"局面输入不是合法 JSON"}); }
  const body = {inputs, package_id: $("#cPkg").value || null,
    as_of: $("#cAsof").value.trim() || null,
    snapshot_hash: $("#cHash").value.trim() || null,
    node_budget: Number($("#cBudget").value || 200000)};
  $("#customDiag").classList.add("hidden");
  try {
    const r = await post("/api/explain", body);
    $("#cHashOut").textContent = "闭包身份 " + r.closure_hash;
    $("#customScore").textContent = JSON.stringify(r.trace.outputs, null, 2);
    renderSteps($("#customSteps"), r.trace.steps);
  } catch (e) {
    $("#customScore").textContent = "（无法求解）";
    $("#customSteps").innerHTML = "";
    renderDiag($("#customDiag"), e.payload, true);
  }
});
$("#btnCustomReport").addEventListener("click", async () => {
  let inputs = JSON.parse($("#cInputs").value);
  const rep = await post("/api/report", {
    inputs, package_id: $("#cPkg").value || null,
    as_of: $("#cAsof").value.trim() || null,
    snapshot_hash: $("#cHash").value.trim() || null,
    node_budget: Number($("#cBudget").value || 200000)});
  download("rule-report-custom.json",
    new Blob([JSON.stringify(rep, null, 2)], {type: "application/json"}));
  toast("已导出报告：含规则包摘要、局面输入与诊断/逐步分数");
});
function renderDiag(el, d, sticky) {
  el.classList.remove("hidden");
  el.innerHTML = `<h4>⚠ 明确诊断：${esc(d.code || "ERROR")}</h4>
    <div>${esc(d.message || "")}</div>
    ${d.detail ? `<pre class='summary'>${esc(JSON.stringify(d.detail, null, 2))}</pre>` : ""}`;
  if (!sticky) toast(d.message || d.code, 4000);
}

// ---------- 补丁包 ----------
async function loadPackages() {
  const data = await api("/api/packages");
  for (const p of data.packages) {
    $("#cPkg").insertAdjacentHTML("beforeend",
      `<option value="${p.package_id}">${p.package_id} · ${esc(p.title)}</option>`);
    $("#sumPkg").insertAdjacentHTML("beforeend",
      `<option value="${p.package_id}">${p.package_id}</option>`);
  }
  $("#pkgList").innerHTML = `<table>
    <tr><th>补丁</th><th>类型</th><th>发布时间(UTC)</th><th>规则数</th>
      <th>预编译闭包身份</th><th>按查询重组合内容身份</th><th>身份校验</th></tr>
    ${data.identity_checks.map(c => `<tr class="pkgok">
      <td><b>${c.package_id}</b></td><td>${esc(c.active_packages_at_release.join(", "))}</td>
      <td>${c.released_at}</td><td>${c.rule_count}</td>
      <td><code>${esc(c.snapshot_closure_hash)}</code></td>
      <td><code>${esc(c.query_content_hash)}</code></td>
      <td class="${c.identity_ok ? "ok" : "bad"}">${c.identity_ok ? "✓ 内容一致" : "✗ 漂移"}</td></tr>`).join("")}
    </table><p class="muted">两列完整闭包哈希不同属正常：scope（package / as_of）进入哈希；
    而“规则内容身份”一致，证明预编译包与按查询组合等价。回放只认快照闭包身份。</p>`;
}
$("#btnSummary").addEventListener("click", async () => {
  const s = await api("/api/packages/" + $("#sumPkg").value + "/summary");
  $("#pkgSummary").textContent = JSON.stringify(s, null, 2);
});

// ---------- 索引管理 ----------
async function refreshIndex() {
  const s = await api("/api/admin/index-status");
  $("#indexStatus").textContent = JSON.stringify(s, null, 2);
}
$("#btnReindex").addEventListener("click", async () => {
  const r = await post("/api/admin/reindex");
  toast("索引已原子重建，文档数 " + r.rebuilt);
  refreshIndex();
});
$("#btnReindexFail").addEventListener("click", async () => {
  try { await post("/api/admin/reindex?fail_after=3"); }
  catch (e) { toast("模拟中断：" + e.payload.message, 4200); }
  refreshIndex();
});

// init
loadExamples().then(() => $("#btnReplay").click());
loadPackages();
refreshIndex();
setTimeout(doSearch, 200);
