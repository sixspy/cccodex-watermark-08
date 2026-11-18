(() => {
  const view = document.getElementById("view");
  const readbar = document.getElementById("readbar");
  const msgEl = document.getElementById("msg");

  const state = {
    tab: "waterline",
    meta: {},
    readDay: {},
    waterline: null,
    cross: null,
    reports: [],
    trace: null,
    traceValue: "WM-ORD-131",
    detail: null,
    detailTab: null,
    stampValue: "",
    stampError: null,
    traceError: "",
  };

  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function setMsg(text) {
    msgEl.textContent = text || "";
  }

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(body.error || "HTTP " + r.status);
      err.status = r.status;
      err.detail = body.detail || null;
      throw err;
    }
    return body;
  }

  function relChip(relation) {
    const kind =
      relation === "跨日" ? "warn" : relation === "未来日" ? "future" : "ok";
    return `<span class="chip ${kind}">${esc(relation || "当日")}</span>`;
  }

  function statusChip(row) {
    const cls = row.status === "已盖印" ? "ok" : row.status === "已作废" ? "warn" : "";
    return `<span class="chip ${cls}">${esc(row.status)}</span>`;
  }

  function modeLabel(mode) {
    return mode === "submit" || !mode ? "提交瞬间读库" : mode;
  }

  /* ---------------- 读日条：四处同源 ---------------- */

  function renderReadbar() {
    const d = state.readDay || {};
    readbar.innerHTML = `
      <div class="readbar">
        <div class="readbar-main">
          <span class="eyebrow">读日</span>
          <strong id="read-day">${esc(d.read_day || "-")}</strong>
          <span class="chip src" id="read-day-source" title="读日来源">
            ${esc(d.read_day_source || "-")}
          </span>
          <span class="chip mode" id="read-day-mode">判定时机：${esc(
            d.read_day_mode_label || d.read_day_mode || "-"
          )}</span>
        </div>
        <form class="readbar-set" data-act="set-read-day">
          <input name="read_day" aria-label="设置读日" placeholder="YYYY-MM-DD"
                 value="${esc(d.read_day || "")}" />
          <button type="submit" class="ghost">设读日</button>
        </form>
      </div>
      <p class="muted small">盖印提交时才读库内业务日；打开页面时的快照不参与判定。</p>`;
  }

  /* ---------------- 批次水线 ---------------- */

  function batchRow(b) {
    const blocked = b.stamp_allowed
      ? ""
      : `<span class="reason" title="${esc(b.block_reason_code)}">${esc(
          b.block_reason
        )}</span>`;
    const note = b.stamp_note ? `<span class="reason">${esc(b.stamp_note)}</span>` : "";
    const stampBtn =
      b.status === "待盖印" && b.stamp_allowed
        ? `<button type="button" class="ghost" data-act="pick" data-id="${b.id}">盖印</button>`
        : "";
    return `<tr class="${b.relation === "跨日" ? "cross" : ""}">
      <td>${b.id}</td>
      <td>${esc(b.table_name)}</td>
      <td>${esc(b.run_code)}</td>
      <td>${esc(b.biz_date)}</td>
      <td>${relChip(b.relation)}</td>
      <td>${statusChip(b)}</td>
      <td>${esc(b.watermark_value || "-")}${
        b.status === "已盖印"
          ? `<span class="chip ${b.stamp_valid ? "ok" : "warn"}" title="同日接受留证">${
              b.stamp_valid ? "有效印" : "无留证"
            }</span>`
          : ""
      }${note}</td>
      <td class="nowrap">${blocked}${stampBtn}
        <button type="button" class="ghost" data-act="detail" data-id="${b.id}">详情</button></td>
    </tr>`;
  }

  function renderWaterline() {
    const line = state.waterline;
    if (!line) return `<p class="muted">加载中…</p>`;
    const counts = line.counts || {};
    const order = ["当日", "跨日", "未来日"];
    const rows = order.flatMap((key) => (line.buckets || {})[key] || []);
    return `
      <h2>批次水线</h2>
      <div class="stats">
        <div class="stat"><span class="eyebrow">当日</span><strong>${counts["当日"] || 0}</strong></div>
        <div class="stat"><span class="eyebrow">跨日</span><strong>${counts["跨日"] || 0}</strong></div>
        <div class="stat"><span class="eyebrow">未来日</span><strong>${counts["未来日"] || 0}</strong></div>
        <div class="stat"><span class="eyebrow">可盖印</span><strong>${line.stampable_count || 0}</strong></div>
        <div class="stat"><span class="eyebrow">无留证已盖印</span><strong>${line.invalid_stamp_count || 0}</strong></div>
      </div>
      <table>
        <thead><tr>
          <th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>日关系</th>
          <th>状态</th><th>水印 / 留证</th><th></th>
        </tr></thead>
        <tbody>${rows.length ? rows.map(batchRow).join("") : '<tr><td colspan="8" class="muted">无批次</td></tr>'}</tbody>
      </table>
      ${renderDetail()}`;
  }

  /* ---------------- 跨日列表 ---------------- */

  function renderCrossDay() {
    const cross = state.cross;
    if (!cross) return `<p class="muted">加载中…</p>`;
    return `
      <h2>跨日批次</h2>
      <p class="muted">业务日早于读日 ${esc(cross.read_day)} 的批次一律不进当日盖印池${
        cross.read_day_source ? `（来源 ${esc(cross.read_day_source)}）` : ""
      }。</p>
      <div class="stats">
        <div class="stat"><span class="eyebrow">跨日批次</span><strong>${cross.count || 0}</strong></div>
        <div class="stat"><span class="eyebrow">未来日批次</span><strong>${cross.future_count || 0}</strong></div>
      </div>
      <table>
        <thead><tr>
          <th>编号</th><th>表</th><th>运行码</th><th>业务日</th><th>日关系</th>
          <th>状态</th><th>拒因</th><th></th>
        </tr></thead>
        <tbody>${(cross.items || [])
          .map(
            (b) => `<tr class="cross">
              <td>${b.id}</td>
              <td>${esc(b.table_name)}</td>
              <td>${esc(b.run_code)}</td>
              <td>${esc(b.biz_date)}</td>
              <td>${relChip(b.relation)}</td>
              <td>${statusChip(b)}</td>
              <td><span class="reason">${esc(b.block_reason || "-")}</span></td>
              <td class="nowrap"><button type="button" class="ghost" data-act="detail" data-id="${b.id}">详情</button></td>
            </tr>`
          )
          .join("") || '<tr><td colspan="8" class="muted">无跨日批次</td></tr>'}</tbody>
      </table>
      ${renderDetail()}`;
  }

  /* ---------------- 盖印详情 ---------------- */

  function renderAudits(detail) {
    const audits = detail.audits || [];
    if (!audits.length) return `<p class="muted small">暂无盖印留证。</p>`;
    return `<table class="audits">
      <thead><tr><th>#</th><th>结果</th><th>读日</th><th>读日来源</th><th>拒因码</th><th>短句</th><th>水印</th><th>时间</th></tr></thead>
      <tbody>${audits
        .map(
          (a) => `<tr class="${a.result === "rejected" ? "cross" : ""}">
            <td>${a.id}</td>
            <td><span class="chip ${a.result === "accepted" ? "ok" : "warn"}">${esc(
            a.result === "accepted" ? "接受" : "拒绝"
          )}</span></td>
            <td>${esc(a.read_day)}</td>
            <td>${esc(a.read_day_source)}</td>
            <td>${esc(a.reason_code || "-")}</td>
            <td>${esc(a.reason || "-")}</td>
            <td>${esc(a.watermark_value || "-")}</td>
            <td>${esc(a.created_at)}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
  }

  function renderDetail() {
    const d = state.detail;
    if (!d || state.detailTab !== state.tab) return "";
    const gate = d.stamp_allowed
      ? `<span class="chip ok">可盖印</span>`
      : `<span class="chip warn">不可盖印</span> <span class="reason" id="detail-reason">${esc(
          d.block_reason || "-"
        )}</span>`;
    const stampBox =
      d.status === "待盖印" && d.stamp_allowed
        ? `<div class="row">
             <input id="stamp-input" value="${esc(state.stampValue)}" placeholder="水印值" />
             <button type="button" data-act="stamp" data-id="${d.id}">写入盖印</button>
           </div>`
        : `<p class="muted small">此批次当前不可写入盖印，按钮已按判定关闭。</p>`;
    const stampErr = state.stampError
      ? `<p class="err" id="stamp-error">${esc(state.stampError.message)}${
          state.stampError.detail
            ? `（拒因码 ${esc(state.stampError.detail.reason_code || "-")} · 读日 ${esc(
                state.stampError.detail.read_day || "-"
              )} · 来源 ${esc(state.stampError.detail.read_day_source || "-")} · 未写入 ${
                state.stampError.detail.watermark_written === false ? "是" : "否"
              }）`
            : ""
        }</p>`
      : "";
    return `<div class="cardish" id="detail">
      <div class="row spread">
        <h3 style="margin:0">盖印详情 · ${esc(d.run_code)}</h3>
        <button type="button" class="ghost" data-act="close-detail">收起</button>
      </div>
      <dl class="kv">
        <dt>表</dt><dd>${esc(d.table_name)}</dd>
        <dt>业务日</dt><dd>${esc(d.biz_date)} ${relChip(d.relation)}</dd>
        <dt>读日</dt><dd>${esc(d.read_day)} · ${esc(d.read_day_source || "-")}</dd>
        <dt>状态</dt><dd>${statusChip(d)}${
      d.status === "已盖印" && !d.stamp_valid
        ? ` <span class="reason">${esc(d.stamp_note || "无同日接受留证")}</span>`
        : ""
    }</dd>
        <dt>判定</dt><dd>${gate}</dd>
      </dl>
      ${stampErr}
      ${stampBox}
      <h4>盖印留证</h4>
      ${renderAudits(d)}
    </div>`;
  }

  /* ---------------- 追索台 ---------------- */

  function renderTrace() {
    const t = state.trace;
    let body = "";
    if (t) {
      const rows = (t.sources || [])
        .map(
          (s) => `<tr>
            <td>${esc(s.run_code)}</td><td>${esc(s.table_name)}</td><td>${esc(s.biz_date)}</td>
            <td>${relChip(s.relation)}</td><td><span class="chip ok">同日接受留证</span></td>
          </tr>`
        )
        .join("");
      const down = (t.downstream || [])
        .map(
          (d) => `<tr class="${d.relation === "跨日" ? "cross" : ""}">
            <td>${esc(d.run_code)}</td><td>${esc(d.table_name)}</td><td>${esc(d.biz_date)}</td>
            <td>${relChip(d.relation)}</td>
            <td>${statusChip(d)}${
            d.stamp_note ? ` <span class="reason">${esc(d.stamp_note)}</span>` : ""
          }</td>
            <td>${
            d.status === "已盖印"
              ? `<span class="chip ${d.stamp_valid ? "ok" : "warn"}">${
                  d.stamp_valid ? "有效" : "无效"
                }</span>`
              : '<span class="muted">未盖印</span>'
          }</td>
          </tr>`
        )
        .join("");
      body = `<div class="cardish">
        <dl class="kv">
          <dt>读日</dt><dd>${esc(t.read_day)}</dd>
          <dt>读日来源</dt><dd>${esc(t.read_day_source)}</dd>
          <dt>判定时机</dt><dd>${esc(modeLabel(t.read_day_mode))}</dd>
        </dl>
        <h4>源批次（只认同日接受留证）</h4>
        <table><thead><tr><th>运行码</th><th>表</th><th>业务日</th><th>日关系</th><th>留证</th></tr></thead>
          <tbody>${rows || '<tr><td colspan="5" class="muted">无</td></tr>'}</tbody></table>
        <h4>下游</h4>
        <table><thead><tr><th>运行码</th><th>表</th><th>业务日</th><th>日关系</th><th>状态</th><th>同日留证</th></tr></thead>
          <tbody>${down || '<tr><td colspan="6" class="muted">无下游批次</td></tr>'}</tbody></table>
      </div>`;
    }
    return `
      <h2>下游追索</h2>
      <form class="row" data-act="trace">
        <input name="watermark_value" value="${esc(state.traceValue)}" placeholder="例如 WM-ORD-131" />
        <button type="submit">追索</button>
      </form>
      ${
        state.traceError
          ? `<p class="err" id="trace-error">${esc(state.traceError)}</p>`
          : ""
      }
      ${body}`;
  }

  /* ---------------- 缺口对账 ---------------- */

  function renderGaps() {
    return `
      <h2>缺口对账</h2>
      <p class="muted">以读日为默认对账日；伪盖印（无同日接受留证）单独计数。</p>
      <form class="row" data-act="gap">
        <input name="as_of_date" placeholder="留空即按读日" />
        <button type="submit">生成对账</button>
      </form>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>读日</th><th>读日来源</th>
          <th>应盖印</th><th>有效盖印</th><th>伪盖印</th><th>跨日</th><th>缺口</th><th>摘要</th></tr></thead>
        <tbody>${state.reports
          .map(
            (r) => `<tr>
              <td>${r.id}</td>
              <td>${esc(r.as_of_date)}</td>
              <td>${esc(r.read_day || "-")}</td>
              <td>${esc(r.read_day_source || "-")}</td>
              <td>${r.expected_count}</td>
              <td>${r.stamped_count}</td>
              <td>${r.invalid_stamp_count || 0}</td>
              <td>${r.cross_day_count || 0}</td>
              <td>${r.gap_count}</td>
              <td>${esc(r.summary)}</td>
            </tr>`
          )
          .join("") || '<tr><td colspan="10" class="muted">暂无对账</td></tr>'}</tbody>
      </table>`;
  }

  function renderTemplates() {
    const items = (state.meta && state.meta.templates) || [];
    return `
      <h2>水印模板</h2>
      <table>
        <thead><tr><th>表</th><th>列</th><th>版本</th><th>状态</th></tr></thead>
        <tbody>${items
          .map(
            (t) => `<tr><td>${esc(t.table_name)}</td><td>${esc(t.column_name)}</td>
              <td>${t.version_no}</td><td><span class="chip">${esc(t.status)}</span></td></tr>`
          )
          .join("")}</tbody>
      </table>`;
  }

  function syncDock() {
    document.querySelectorAll(".dock [data-tab]").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.tab === state.tab);
    });
  }

  function render() {
    syncDock();
    renderReadbar();
    if (state.tab === "waterline") view.innerHTML = renderWaterline();
    else if (state.tab === "crossday") view.innerHTML = renderCrossDay();
    else if (state.tab === "trace") view.innerHTML = renderTrace();
    else if (state.tab === "gaps") view.innerHTML = renderGaps();
    else view.innerHTML = renderTemplates();
  }

  /* ---------------- 数据 ---------------- */

  async function refreshAll() {
    const meta = await api("/api/meta");
    state.meta = { ...meta, templates: (await api("/api/templates")).items || [] };
    state.readDay = {
      read_day: meta.read_day,
      read_day_source: meta.read_day_source,
      read_day_mode: meta.read_day_mode,
      read_day_mode_label: meta.read_day_mode_label,
    };
    state.waterline = await api("/api/waterline");
    state.cross = await api("/api/batches/cross-day");
    state.reports = (await api("/api/gap-reports")).items || [];
  }

  async function openDetail(id) {
    const body = await api("/api/batches/" + id);
    state.detail = body.batch;
    state.detailTab = state.tab;
    state.stampValue = "WM-" + String(state.detail.run_code || "").toUpperCase();
    state.stampError = null;
  }

  /* ---------------- 事件 ---------------- */

  document.addEventListener("click", (ev) => {
    const el = ev.target.closest("[data-act]");
    if (!el) return;
    const act = el.dataset.act;
    if (act === "tab") {
      state.tab = el.dataset.tab;
      render();
    } else if (act === "logout") {
      fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
    } else if (act === "pick" || act === "detail") {
      openDetail(Number(el.dataset.id))
        .then(() => render())
        .catch((e) => setMsg(e.message));
    } else if (act === "close-detail") {
      state.detail = null;
      state.detailTab = null;
      state.stampError = null;
      render();
    } else if (act === "stamp") {
      doStamp(Number(el.dataset.id));
    }
  });

  document.addEventListener("input", (ev) => {
    if (ev.target.id === "stamp-input") state.stampValue = ev.target.value;
  });

  document.addEventListener("submit", (ev) => {
    const form = ev.target.closest("form[data-act]");
    if (!form) return;
    ev.preventDefault();
    const act = form.dataset.act;
    const fd = new FormData(form);
    if (act === "set-read-day") {
      api("/api/read-day", {
        method: "POST",
        body: JSON.stringify({ read_day: String(fd.get("read_day") || "").trim() }),
      })
        .then(async () => {
          await refreshAll();
          setMsg("读日已切换，四处视图同源刷新");
          render();
        })
        .catch((e) => setMsg(e.message));
    } else if (act === "trace") {
      const value = String(fd.get("watermark_value") || "").trim();
      state.traceValue = value;
      state.traceError = "";
      api("/api/trace", { method: "POST", body: JSON.stringify({ watermark_value: value }) })
        .then((r) => {
          state.trace = r;
          setMsg("追索完成");
          render();
        })
        .catch((e) => {
          const d = e.detail || {};
          // 拒因也要亮出来：读日来源仍取自服务端 detail，而不是页面快照。
          state.trace = {
            read_day: d.read_day || state.readDay.read_day,
            read_day_source: d.read_day_source || state.readDay.read_day_source,
            read_day_mode: d.read_day_mode || state.readDay.read_day_mode,
            sources: [],
            downstream: [],
          };
          state.traceError = e.message;
          setMsg(e.message);
          render();
        });
    } else if (act === "gap") {
      api("/api/gap-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date: String(fd.get("as_of_date") || "").trim() }),
      })
        .then(async (r) => {
          state.reports = (await api("/api/gap-reports")).items || [];
          setMsg(r.report.summary);
          render();
        })
        .catch((e) => setMsg(e.message));
    }
  });

  async function doStamp(id) {
    setMsg("");
    state.stampError = null;
    try {
      await api("/api/batches/" + id + "/stamp", {
        method: "POST",
        body: JSON.stringify({ watermark_value: state.stampValue }),
      });
      await refreshAll();
      state.detail = (await api("/api/batches/" + id)).batch;
      state.detailTab = state.tab;
      setMsg("盖印完成，留证已写入");
      render();
    } catch (e) {
      state.stampError = e;
      setMsg(e.message);
      try {
        state.detail = (await api("/api/batches/" + id)).batch;
        state.detailTab = state.tab;
      } catch (_) {
        /* 保持原详情 */
      }
      render();
    }
  }

  refreshAll()
    .then(() => {
      const pool = (state.waterline.buckets || {})["当日"] || [];
      const first = pool.find((b) => b.stamp_allowed);
      if (first) return openDetail(first.id);
      return null;
    })
    .then(() => render())
    .catch((e) => {
      render();
      setMsg(e.message);
    });
})();
