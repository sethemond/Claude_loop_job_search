// Run tab — loaded eagerly via <script defer> so all functions are on window
// before DOMContentLoaded fires. Reads globals from dashboard.html:
//   SERVER, serverOk, esc(), today(), loadData(), showTab(),
//   startBatchPoll(), stopBatchPoll(), runTabVisible, currentRunState

// ── Timezone utilities ────────────────────────────────────────────────────────

let _displayTz = Intl.DateTimeFormat().resolvedOptions().timeZone;

function _tzAbbr() {
  return new Date().toLocaleTimeString('en-US', { timeZone: _displayTz, timeZoneName: 'short' }).split(' ').pop();
}

function _localToUtc(h, m) {
  const d = new Date(); d.setHours(h, m, 0, 0);
  return { h: d.getUTCHours(), m: d.getUTCMinutes() };
}

function _utcToLocal(h, m) {
  const d = new Date(); d.setUTCHours(h, m, 0, 0);
  return { h: d.getHours(), m: d.getMinutes() };
}

function _fmtUtcIso(isoStr) {
  if (!isoStr) return '—';
  const d = new Date(isoStr.endsWith('Z') || isoStr.includes('+') ? isoStr : isoStr + 'Z');
  return d.toLocaleString('en-US', {
    timeZone: _displayTz,
    month: 'numeric', day: 'numeric',
    hour: '2-digit', minute: '2-digit',
    timeZoneName: 'short',
  });
}

// ── Log polling ───────────────────────────────────────────────────────────────

let logPollInterval = null;

function startLogPoll() {
  if (logPollInterval) return;
  logPollInterval = setInterval(fetchAndShowLog, 3000);
}

function stopLogPoll() {
  if (logPollInterval) { clearInterval(logPollInterval); logPollInterval = null; }
}

async function fetchAndShowLog() {
  if (!serverOk) return;
  try {
    const d = await fetch(SERVER+'/loop-log').then(r=>r.json());
    const pre = document.getElementById('run-log-pre');
    if (!pre) return;
    const atBottom = pre.scrollHeight - pre.clientHeight <= pre.scrollTop + 60;
    pre.textContent = (d.lines || []).join('\n') || 'No loop log yet.';
    if (atBottom) pre.scrollTop = pre.scrollHeight;
    const fn = document.getElementById('run-log-filename');
    if (fn && d.file) fn.textContent = d.file;
  } catch {}
}

function scrollLogToBottom() {
  const pre = document.getElementById('run-log-pre');
  if (pre) pre.scrollTop = pre.scrollHeight;
}

// ── Queue Manager data + reorder panel ───────────────────────────────────────

let queueMgrData  = null;
let queueMgrOpen  = false; // referenced by dashboard.html:1166; kept for compat (Reorder mode has no separate toggle)
let _dragSrcId    = null;
let _pendingReorder = null;

async function loadQueueMgr() {
  if (!serverOk) return;
  try {
    queueMgrData = await fetch(SERVER+'/queue-manage').then(r=>r.json());
    renderQsReorder();
  } catch {}
}

function _queueSortKey(e) {
  return e.order != null ? e.order : e.id;
}

function renderQsReorder() {
  const listEl = document.getElementById('qs-reorder-list');
  const summEl = document.getElementById('qs-reorder-summary');
  const cntEl  = document.getElementById('qs-reorder-counts');
  if (!listEl) return;
  if (!queueMgrData) { listEl.innerHTML = '<div class="queue-empty">Loading…</div>'; return; }

  const q       = [...(queueMgrData.queue || [])].sort((a,b) => _queueSortKey(a) - _queueSortKey(b));
  const filter  = (document.getElementById('qs-reorder-filter')?.value || '').toLowerCase();
  const filtered = filter
    ? q.filter(e => (e.location||'').toLowerCase().includes(filter) || (e.keyword||'').toLowerCase().includes(filter))
    : q;

  const total   = q.length;
  const skCount = q.filter(e=>e.skip_next).length;
  if (summEl) summEl.textContent = `${total} entries${skCount ? ` · ${skCount} skipped` : ''} — drag rows to reorder`;
  if (cntEl)  cntEl.textContent  = filter ? `${filtered.length} of ${total} shown` : '';

  // Also update the qs-count-label if we're in reorder mode
  const lbl = document.getElementById('qs-count-label');
  if (lbl && qsMode === 'reorder') lbl.textContent = `(${total} total)`;

  listEl.innerHTML = filtered.length
    ? filtered.map(e => queueRowHtml(e)).join('')
    : '<div class="queue-empty">No entries match filter.</div>';

  listEl.querySelectorAll('.queue-row[draggable]').forEach(row => {
    row.addEventListener('dragstart', onDragStart);
    row.addEventListener('dragover',  onDragOver);
    row.addEventListener('dragleave', onDragLeave);
    row.addEventListener('drop',      onDrop);
    row.addEventListener('dragend',   onDragEnd);
  });
}

function queueRowHtml(e) {
  const age     = e._days_since != null ? `${e._days_since}d ago` : '';
  const skipped = !!e.skip_next;
  const pillCls = skipped ? 'qsp-skipped' : e.status==='pending' ? 'qsp-pending' : e._due ? 'qsp-due' : 'qsp-done';
  const pillTxt = skipped ? 'SKIP' : e.status==='pending' ? 'NEW' : e._due ? 'DUE' : 'DONE';
  const skipBtn = skipped
    ? `<button class="btn-qunskip" onclick="qToggleSkip(${e.id})">↩</button>`
    : `<button class="btn-qskip"   onclick="qToggleSkip(${e.id})" title="Skip next batch">⊘</button>`;
  const queueBtn = e.status==='done'
    ? `<button class="btn-qqueue" onclick="qSetPending(${e.id})" title="Queue now">+</button>`
    : '';
  return `<div class="queue-row${skipped?' skipped':''}" id="qrow-${e.id}" data-id="${e.id}" draggable="true">
    <span class="queue-drag-handle" title="Drag to reorder">⠿</span>
    <span class="queue-loc" title="${esc(e.location)}">${esc(e.location)}</span>
    <span class="queue-kw"  title="${esc(e.keyword)}">${esc(e.keyword)}</span>
    <span class="queue-status-pill ${pillCls}">${pillTxt}</span>
    <span class="queue-age">${age}</span>
    <div class="queue-actions">
      ${skipBtn}
      ${queueBtn}
      <button class="btn-qremove" onclick="qRemove(${e.id})" title="Permanently remove">✕</button>
    </div>
  </div>`;
}

// ── Drag-and-drop handlers ────────────────────────────────────────────────────

function onDragStart(e) {
  _dragSrcId = parseInt(this.dataset.id, 10);
  this.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', _dragSrcId);
}

function onDragOver(e) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  const target = e.currentTarget;
  if (target.dataset.id && parseInt(target.dataset.id,10) !== _dragSrcId) {
    target.classList.add('drag-over');
  }
}

function onDragLeave(e) {
  e.currentTarget.classList.remove('drag-over');
}

function onDrop(e) {
  e.preventDefault();
  const target = e.currentTarget;
  target.classList.remove('drag-over');
  const srcId = _dragSrcId;
  const tgtId = parseInt(target.dataset.id, 10);
  if (!srcId || srcId === tgtId) return;

  const listEl = document.getElementById('qs-reorder-list');
  const srcRow = document.getElementById(`qrow-${srcId}`);
  const tgtRow = document.getElementById(`qrow-${tgtId}`);
  if (!srcRow || !tgtRow) return;

  const srcIdx = [...listEl.children].indexOf(srcRow);
  const tgtIdx = [...listEl.children].indexOf(tgtRow);
  if (srcIdx < tgtIdx) {
    tgtRow.after(srcRow);
  } else {
    tgtRow.before(srcRow);
  }

  if (_pendingReorder) clearTimeout(_pendingReorder);
  _pendingReorder = setTimeout(() => saveQueueOrder(listEl), 300);
}

function onDragEnd() {
  this.classList.remove('dragging');
  document.querySelectorAll('.queue-row.drag-over').forEach(r => r.classList.remove('drag-over'));
  _dragSrcId = null;
}

async function saveQueueOrder(listEl) {
  if (!serverOk) return;
  const ids = [...listEl.querySelectorAll('.queue-row[data-id]')].map(r => parseInt(r.dataset.id, 10));
  try {
    const r = await fetch(SERVER+'/queue-reorder', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({order: ids}),
    });
    if (r.ok) {
      if (queueMgrData) {
        const orderMap = Object.fromEntries(ids.map((id, idx) => [id, idx]));
        queueMgrData.queue.forEach(e => { if (e.id in orderMap) e.order = orderMap[e.id]; });
      }
      refreshQueuePreview();
    }
  } catch {}
}

// ── Queue actions ─────────────────────────────────────────────────────────────

async function qToggleSkip(id) {
  if (!serverOk) return;
  try {
    const r = await fetch(SERVER+'/queue-toggle-skip', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({id}),
    });
    if (r.ok) { await loadQueueMgr(); refreshQueuePreview(); }
  } catch {}
}

async function qSetPending(id) {
  if (!serverOk) return;
  try {
    const r = await fetch(SERVER+'/queue-set-status', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({id, status:'pending'}),
    });
    if (r.ok) { await loadQueueMgr(); refreshQueuePreview(); }
  } catch {}
}

async function qRemove(id) {
  if (!serverOk) return;
  const entry = (queueMgrData?.queue||[]).find(e=>e.id===id);
  const label = entry ? `"${entry.location} — ${entry.keyword}"` : `entry #${id}`;
  if (!confirm(`Permanently remove ${label} from the queue?`)) return;
  try {
    const r = await fetch(SERVER+'/queue-remove', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({id}),
    });
    if (r.ok) { await loadQueueMgr(); refreshQueuePreview(); }
  } catch {}
}

// ── Queue preview (data fetch + auto-panel render) ────────────────────────────

let qsPreviewData = null;

async function refreshQueuePreview() {
  if (!serverOk) return;
  try {
    qsPreviewData = await fetch(SERVER+'/queue-preview').then(r=>r.json());
    // Keep qsN in sync with the server's batch_size (source of truth for how many entries are returned)
    const serverN = qsPreviewData?.settings?.batch_size;
    if (serverN != null && serverN !== qsN) {
      qsN = serverN;
      const nEl = document.getElementById('qs-n-val');
      if (nEl) nEl.textContent = qsN;
    }
    renderQsAutoPreview();
  } catch {}
}

// Alias so dashboard.html callers still work after rename
function loadQueuePreview() { return refreshQueuePreview(); }

// ── Abort batch ───────────────────────────────────────────────────────────────

async function abortRun() {
  if (!serverOk) return;
  if (!confirm('Stop the running batch?')) return;
  try {
    await fetch(SERVER+'/run-stop', {method:'POST'});
    stopBatchPoll();
    stopLogPoll();
    await fetchAndShowLog();
    checkRunStatus();
  } catch {}
}

// ── Recent Runs ───────────────────────────────────────────────────────────────

let runsHistOpen = true;

function toggleRunsHist() {
  runsHistOpen = !runsHistOpen;
  document.getElementById('runs-hist-body').classList.toggle('open', runsHistOpen);
  document.getElementById('runs-hist-chevron').textContent = runsHistOpen ? '▼' : '▶';
}

async function loadRunHistory() {
  if (!serverOk) return;
  try {
    const d = await fetch(SERVER+'/runs').then(r=>r.json());
    renderRunHistory(d);
  } catch {}
}

function renderRunHistory(d) {
  const listEl = document.getElementById('runs-hist-list');
  const summEl = document.getElementById('runs-hist-summary');
  if (!listEl) return;

  const runs = Array.isArray(d?.runs) ? d.runs : [];
  const sorted = [...runs].reverse().slice(0, 20);

  if (summEl) {
    summEl.textContent = runs.length ? `${runs.length} total` : '';
  }

  if (!sorted.length) {
    listEl.innerHTML = '<div style="font-size:12px;color:var(--muted);padding:8px 0">No completed runs yet.</div>';
    return;
  }

  listEl.innerHTML = sorted.map(r => runHistRow(r)).join('');
}

function runHistRow(r) {
  const status      = r.status || 'done';
  const isDismissed = !!r.dismissed;

  let pillCls = 'rh-pill-done', pillTxt = 'DONE';
  if (status === 'error')             { pillCls = 'rh-pill-error'; pillTxt = 'ERROR'; }
  else if (status === 'rate_limited') { pillCls = 'rh-pill-rl';    pillTxt = 'PAUSED'; }

  let search = '';
  if (r.keyword && r.location) {
    search = `${esc(r.keyword)} @ ${esc(r.location)}`;
  } else if (r.searches_run != null) {
    search = `${r.searches_run} search${r.searches_run !== 1 ? 'es' : ''}`;
  } else {
    search = '—';
  }
  const promptFile  = r.primary_prompt_file || '';
  const promptBadge = promptFile
    ? `<span style="font-size:10px;color:var(--muted);margin-left:5px;font-weight:400">[${esc(promptFile)}]</span>`
    : '';

  const added    = r.jobs_added != null ? r.jobs_added : (r.jobs_new != null ? r.jobs_new : null);
  const addedTxt = added != null ? (added > 0 ? `+${added}` : '±0') : '—';
  const addedCls = added ? '' : ' zero';

  let sessTxt = '—';
  if (r.session_reused === true)       sessTxt = 'reused';
  else if (r.session_reused === false) sessTxt = 'fresh';

  let costTxt = '—';
  if (r.batch_cost_usd != null) {
    const sessStr = r.batch_pct_session != null ? `${(r.batch_pct_session*100).toFixed(1)}%` : '?';
    const weekStr = r.batch_pct_weekly  != null ? `${(r.batch_pct_weekly *100).toFixed(2)}%` : '?';
    costTxt = `$${r.batch_cost_usd.toFixed(3)} · ${sessStr} · ${weekStr}`;
  }

  const timeStr = r.started ? r.started.slice(11,16) : '';

  let actionBtns = '';
  if (!isDismissed) {
    const canContinue = (status === 'error' || status === 'rate_limited') && r.session_id;
    if (canContinue) {
      actionBtns += `<button class="rh-continue-btn" onclick="continueRun(${JSON.stringify(r.session_id)},${JSON.stringify(r.entry_id)},${JSON.stringify(r.keyword||'')},${JSON.stringify(r.location||'')})">↩ Continue</button>`;
    }
    if (status === 'error' || status === 'rate_limited') {
      actionBtns += `<button class="rh-dismiss-btn" title="Dismiss" onclick="dismissRun(${JSON.stringify(r.started)}, this)">✕</button>`;
    }
  }

  const rowCls      = isDismissed ? ' dismissed' : '';
  const searchTitle = promptFile ? `${search} [${promptFile}]` : search;
  return `<div class="rh-row${rowCls}">
    <span class="rh-pill ${pillCls}">${pillTxt}</span>
    <span class="rh-search" title="${searchTitle}">${search}${promptBadge}</span>
    <span class="rh-jobs${addedCls}">${addedTxt}</span>
    <span class="rh-sess">${sessTxt}</span>
    <span class="rh-cost">${costTxt}</span>
    <span class="rh-time">${timeStr}</span>
    ${actionBtns}
  </div>`;
}

async function dismissRun(started, btnEl) {
  try {
    const r = await fetch(SERVER + '/dismiss-run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ started }),
    });
    if (r.ok) {
      const row = btnEl?.closest('.rh-row');
      if (row) {
        row.classList.add('dismissed');
        row.querySelectorAll('.rh-continue-btn, .rh-dismiss-btn').forEach(b => b.remove());
      }
    }
  } catch(e) {}
}

async function continueRun(sessionId, entryId, keyword, location) {
  if (!confirm(`Resume session ${sessionId.slice(0,8)}… for "${keyword} @ ${location}"?`)) return;
  try {
    const r = await fetch(SERVER + '/continue-run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        session_id: sessionId,
        entry: { id: entryId, keyword, location, status: 'pending' },
      }),
    });
    if (r.ok) {
      showTab('run');
      startBatchPoll();
    } else {
      const d = await r.json().catch(() => ({}));
      alert('Continue failed: ' + (d.error || r.status));
    }
  } catch(e) { alert('Continue error: ' + e); }
}

// ── Automatic Runs — Loop & Schedule ─────────────────────────────────────────

let loopPollInterval = null;
let qsMode           = 'auto';
let qsN              = 5;
let qsManualSelected = new Set();
let schedPattern     = 'daily';
let limitsOpen       = false;
let primaryPromptFile = 'workflow.md';

// ── Primary Prompt ────────────────────────────────────────────────────────────

async function loadMdFiles() {
  const sel = document.getElementById('primary-prompt-file');
  if (!sel) return;
  if (!serverOk) {
    sel.innerHTML = '<option value="" disabled selected>Server offline — cannot load files</option>';
    return;
  }
  try {
    const d = await fetch(SERVER + '/list-md-files').then(r => r.json());
    const files = d.files;
    if (!files || !files.length) {
      sel.innerHTML = '<option value="" disabled selected>No .md files found</option>';
      return;
    }
    // Default to workflow.md if it exists and nothing is selected yet
    if (!primaryPromptFile || !files.includes(primaryPromptFile)) {
      primaryPromptFile = files.includes('workflow.md') ? 'workflow.md' : files[0];
    }
    sel.innerHTML = files.map(f =>
      `<option value="${esc(f)}" ${f === primaryPromptFile ? 'selected' : ''}>${esc(f)}</option>`
    ).join('');
  } catch {
    sel.innerHTML = '<option value="" disabled selected>Error loading files</option>';
  }
}

function primaryPromptChanged() {
  primaryPromptFile = document.getElementById('primary-prompt-file')?.value || 'workflow.md';
}

// ── Queue selection ───────────────────────────────────────────────────────────

function setQsMode(mode) {
  qsMode = mode;
  document.getElementById('qs-btn-auto').classList.toggle('active', mode === 'auto');
  document.getElementById('qs-btn-manual').classList.toggle('active', mode === 'manual');
  document.getElementById('qs-btn-reorder').classList.toggle('active', mode === 'reorder');
  document.getElementById('qs-auto-panel').style.display    = mode === 'auto'    ? '' : 'none';
  document.getElementById('qs-manual-panel').style.display  = mode === 'manual'  ? '' : 'none';
  document.getElementById('qs-reorder-panel').style.display = mode === 'reorder' ? '' : 'none';
  if ((mode === 'manual' || mode === 'reorder') && !queueMgrData) loadQueueMgr();
  if (mode === 'manual')  renderQsManual();
  if (mode === 'reorder') renderQsReorder();
}

async function qsAdjustN(delta) {
  qsN = Math.max(1, Math.min(50, qsN + delta));
  document.getElementById('qs-n-val').textContent = qsN;
  // Persist to server — batch_size is what caps /queue-preview results
  try {
    if (serverOk) {
      await fetch(SERVER + '/queue-settings', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({batch_size: qsN}),
      });
    }
  } catch {}
  refreshQueuePreview();
}

function renderQsAutoPreview() {
  const el      = document.getElementById('qs-auto-preview');
  const statsEl = document.getElementById('qs-auto-stats');
  const lbl     = document.getElementById('qs-count-label');
  if (!el) return;

  if (!qsPreviewData) {
    if (statsEl) statsEl.textContent = '';
    el.textContent = 'Loading queue…';
    return;
  }

  const total = qsPreviewData.total || 0;
  const pc    = qsPreviewData.pending_count || 0;
  const dc    = qsPreviewData.due_count     || 0;
  const sc    = qsPreviewData.skipped_count || 0;

  if (statsEl) {
    const parts = [`${total} total`, `${pc} pending`, `${dc} due`];
    if (sc) parts.push(`${sc} skipped`);
    statsEl.textContent = parts.join(' · ');
  }

  const sp     = qsPreviewData.selected_pending || [];
  const sd     = qsPreviewData.selected_due     || [];
  const spSlice = sp.slice(0, qsN);
  const sdSlice = sd.slice(0, Math.max(0, qsN - spSlice.length));
  const shown   = spSlice.length + sdSlice.length;

  if (lbl && qsMode === 'auto') lbl.textContent = total > 0 ? `(${shown} of ${total} total)` : '';

  if (!shown) {
    el.innerHTML = `<div style="font-size:11px;color:var(--muted)">Nothing pending in queue.</div>`;
    return;
  }

  let html = '';
  if (spSlice.length) {
    html += `<div style="font-size:10px;color:var(--blue);font-weight:700;margin:4px 0 2px;text-transform:uppercase;letter-spacing:.5px">New</div>`;
    html += spSlice.map(e =>
      `<div class="preview-row">
        <span class="preview-loc">${esc(e.location)}</span>
        <span class="preview-kw">${esc(e.keyword)}</span>
        <span class="preview-badge pending">NEW</span>
      </div>`
    ).join('');
  }
  if (sdSlice.length) {
    html += `<div style="font-size:10px;color:var(--yellow);font-weight:700;margin:4px 0 2px;text-transform:uppercase;letter-spacing:.5px">Due</div>`;
    html += sdSlice.map(e =>
      `<div class="preview-row">
        <span class="preview-loc">${esc(e.location)}</span>
        <span class="preview-kw">${esc(e.keyword)}</span>
        <span class="preview-badge due">${e._days_since}d ago</span>
      </div>`
    ).join('');
  }
  el.innerHTML = html;
}

function renderQsManual() {
  const el     = document.getElementById('qs-manual-list');
  const summEl = document.getElementById('qs-manual-summary');
  if (!el || !queueMgrData) return;
  const filter = (document.getElementById('qs-manual-filter')?.value || '').toLowerCase();
  const q = [...(queueMgrData.queue || [])].sort((a, b) => (a.order ?? a.id) - (b.order ?? b.id));

  const groups = {};
  q.forEach(e => {
    if (filter && !(e.keyword||'').toLowerCase().includes(filter) && !(e.location||'').toLowerCase().includes(filter)) return;
    const kw = e.keyword || 'Other';
    if (!groups[kw]) groups[kw] = [];
    groups[kw].push(e);
  });

  let html = '';
  for (const [kw, entries] of Object.entries(groups)) {
    const allSel = entries.every(e => qsManualSelected.has(e.id));
    html += `<div class="qs-group-hdr">
      <input type="checkbox" ${allSel?'checked':''} onchange="qsToggleGroup(${JSON.stringify(entries.map(e=>e.id))}, this.checked)" style="accent-color:var(--blue)">
      <span>${esc(kw)}</span>
      <span style="margin-left:auto;color:var(--muted)">${entries.length}</span>
    </div>`;
    html += entries.map(e => {
      const pillCls = e.skip_next ? 'qs-badge-done' : e.status==='pending' ? 'qs-badge-pending' : e._due ? 'qs-badge-due' : 'qs-badge-done';
      const pillTxt = e.skip_next ? 'SKIP' : e.status==='pending' ? 'NEW' : e._due ? 'DUE' : 'DONE';
      return `<label class="qs-entry-row">
        <input type="checkbox" ${qsManualSelected.has(e.id)?'checked':''} onchange="qsToggleEntry(${e.id}, this.checked)" style="accent-color:var(--blue)">
        <span class="qs-entry-loc">${esc(e.location)}</span>
        <span style="color:var(--muted);flex:1;font-size:11px">${esc(e.keyword)}</span>
        <span class="qs-entry-badge ${pillCls}">${pillTxt}</span>
      </label>`;
    }).join('');
  }

  el.innerHTML = html || '<div style="padding:8px 10px;font-size:12px;color:var(--muted)">No entries match filter.</div>';
  const n = qsManualSelected.size;
  if (summEl) summEl.textContent = n > 0 ? `${n} search${n>1?'es':''} selected` : 'No searches selected';
  const lbl = document.getElementById('qs-count-label');
  if (lbl && qsMode === 'manual') lbl.textContent = n > 0 ? `(${n} selected)` : '';
}

function qsToggleEntry(id, checked) {
  if (checked) qsManualSelected.add(id); else qsManualSelected.delete(id);
  renderQsManual();
}

function qsToggleGroup(ids, checked) {
  ids.forEach(id => { if (checked) qsManualSelected.add(id); else qsManualSelected.delete(id); });
  renderQsManual();
}

function qsSelectAll()  { (queueMgrData?.queue || []).forEach(e => qsManualSelected.add(e.id)); renderQsManual(); }
function qsSelectNone() { qsManualSelected.clear(); renderQsManual(); }

function _getSelectedQueueIds() {
  if (qsMode === 'manual') return [...qsManualSelected];
  // Prefer granular pending/due lists; fall back to combined 'selected'
  const sp = qsPreviewData?.selected_pending || [];
  const sd = qsPreviewData?.selected_due     || [];
  if (sp.length || sd.length) {
    const spSlice = sp.slice(0, qsN);
    const sdSlice = sd.slice(0, Math.max(0, qsN - spSlice.length));
    return [...spSlice, ...sdSlice].map(e => e.id);
  }
  return (qsPreviewData?.selected || []).slice(0, qsN).map(e => e.id);
}

// ── Schedule timing ───────────────────────────────────────────────────────────

function schedWhenChanged() {
  const isScheduled = document.querySelector('input[name="sched-when"]:checked')?.value === 'scheduled';
  document.getElementById('sched-time-panel').style.display = isScheduled ? '' : 'none';
  document.getElementById('btn-schedule-it').style.display  = isScheduled ? '' : 'none';
  document.getElementById('btn-loop-now').textContent = isScheduled ? '▶ Run Now Too' : '▶ Run Now';
}

function setSchedPattern(pattern) {
  schedPattern = pattern;
  document.querySelectorAll('.sched-pattern-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.pattern === pattern));
  document.getElementById('sched-weekday-sel').style.display = pattern === 'weekly' ? 'inline-block' : 'none';
}

function toggleLimits() {
  limitsOpen = !limitsOpen;
  document.getElementById('limits-body').classList.toggle('open', limitsOpen);
  document.getElementById('limits-chevron').textContent = limitsOpen ? '▼' : '▶';
}

function limAutoRescheduleChanged() {
  const on = document.getElementById('lim-auto-reschedule').checked;
  document.getElementById('lim-repeat-row').style.display = on ? '' : 'none';
}

// ── Timezone picker ───────────────────────────────────────────────────────────

function openTzPicker() {
  const picker = document.getElementById('sched-tz-picker');
  if (!picker) return;
  picker.value = _displayTz;
  const isOpen = picker.style.display !== 'none';
  picker.style.display = isOpen ? 'none' : 'block';
  if (!isOpen) {
    setTimeout(() => {
      document.addEventListener('click', function closePicker(e) {
        if (e.target.id !== 'sched-tz-badge' && e.target.id !== 'sched-tz-picker') {
          picker.style.display = 'none';
          document.removeEventListener('click', closePicker);
        }
      });
    }, 0);
  }
}

function applyTzPicker() {
  const picker = document.getElementById('sched-tz-picker');
  if (!picker) return;
  _displayTz = picker.value;
  picker.style.display = 'none';
  const badge = document.getElementById('sched-tz-badge');
  if (badge) badge.textContent = _tzAbbr();
  loadSchedules();
}

// ── Payload builder ───────────────────────────────────────────────────────────

function _buildSchedulePayload(queueIds) {
  const localH = parseInt(document.getElementById('sched-hour-new')?.value   || '0', 10);
  const localM = parseInt(document.getElementById('sched-minute-new')?.value || '0', 10);
  const utc    = _localToUtc(localH, localM);
  return {
    queue_ids:           queueIds,
    session_threshold:   parseFloat(document.getElementById('lim-session')?.value || '80'),
    weekly_threshold:    parseFloat(document.getElementById('lim-weekly')?.value  || '60'),
    context_threshold:   parseFloat(document.getElementById('lim-context')?.value || '90'),
    allow_reschedule:    document.getElementById('lim-auto-reschedule')?.checked ?? false,
    repeat:              parseInt(document.getElementById('lim-repeat')?.value || '0', 10),
    hour_utc:            utc.h,
    minute_utc:          utc.m,
    repeat_pattern:      schedPattern,
    weekday:             parseInt(document.getElementById('sched-weekday-sel')?.value || '0', 10),
    primary_prompt_file: primaryPromptFile,
  };
}

// ── Action handlers ───────────────────────────────────────────────────────────

async function doRunNow() {
  if (!serverOk) { alert('Server must be running.'); return; }
  const ids = _getSelectedQueueIds();
  if (!ids.length) { alert('Select at least one search from the queue.'); return; }
  const isScheduled = document.querySelector('input[name="sched-when"]:checked')?.value === 'scheduled';
  const payload = { ..._buildSchedulePayload(ids), now: true };
  if (isScheduled) {
    const h = parseInt(document.getElementById('sched-hour-new')?.value, 10);
    if (isNaN(h) || h < 0 || h > 23) { alert('Enter a valid hour (0–23) for the schedule.'); return; }
  }
  await _postScheduleLoop(payload);
}

async function doScheduleIt() {
  if (!serverOk) { alert('Server must be running.'); return; }
  const ids = _getSelectedQueueIds();
  if (!ids.length) { alert('Select at least one search from the queue.'); return; }
  const h = parseInt(document.getElementById('sched-hour-new')?.value, 10);
  if (isNaN(h) || h < 0 || h > 23) { alert('Enter a valid hour (0–23) to schedule.'); return; }
  await _postScheduleLoop(_buildSchedulePayload(ids));
}

async function _postScheduleLoop(payload) {
  try {
    const r = await fetch(SERVER + '/schedule-loop', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (r.status === 202) {
      if (payload.now) { startLoopPoll(); await loadLoopStatus(); fetchAndShowLog(); }
      await loadSchedules();
    } else if (r.status === 409) {
      alert('A loop is already running.');
    } else {
      alert('Error: ' + (d.error || 'Unknown') + '\n\nCheck that queue entries are selected.');
    }
  } catch(e) { alert('Could not reach server: ' + e.message); }
}

async function resumeSchedule(schedId) {
  if (!serverOk) return;
  try {
    const d = await fetch(SERVER + '/schedules').then(r => r.json());
    const entry = (d.schedules || []).find(s => s.id === schedId);
    if (!entry) { alert('Schedule not found.'); return; }
    const remaining = entry.remaining_loop_prompts || [];
    if (!remaining.length) { alert('No remaining searches to resume.'); return; }
    await _postScheduleLoop({
      queue_ids: [],
      remaining_loop_prompts: remaining,
      session_id: entry.session_id,
      continuing_from_limit_reached: true,
      now: true,
      session_threshold: entry.settings?.session_threshold ?? 80,
      weekly_threshold:  entry.settings?.weekly_threshold  ?? 60,
      context_threshold: entry.settings?.context_threshold ?? 90,
      allow_reschedule:  entry.allow_reschedule ?? false,
      repeat: 0,
    });
  } catch(e) { alert('Resume error: ' + e.message); }
}

async function returnToQueue(schedId) {
  if (!confirm('Reset remaining searches back to pending in the queue?')) return;
  try {
    const r = await fetch(SERVER + '/return-remaining-to-queue', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: schedId}),
    });
    const d = await r.json();
    if (r.ok) {
      await Promise.all([loadSchedules(), loadQueueMgr(), refreshQueuePreview()]);
    } else {
      alert('Error: ' + (d.error || r.status));
    }
  } catch(e) { alert('Error: ' + e.message); }
}

// ── Loop status polling ───────────────────────────────────────────────────────

async function loadLoopStatus() {
  if (!serverOk) return;
  try {
    const d = await fetch(SERVER+'/loop-status').then(r=>r.json());
    renderLoopStatus(d);
    if (d.status === 'running') startLoopPoll();
    else stopLoopPoll();
  } catch {}
}

async function loadSchedules() {
  if (!serverOk) return;
  try {
    const d = await fetch(SERVER+'/schedules').then(r=>r.json());
    renderSchedules(d);
  } catch {}
}

function startLoopPoll() {
  if (loopPollInterval) return;
  loopPollInterval = setInterval(async () => {
    try {
      const [ls, sc] = await Promise.all([
        fetch(SERVER+'/loop-status').then(r=>r.json()),
        fetch(SERVER+'/schedules').then(r=>r.json()),
      ]);
      renderLoopStatus(ls);
      renderSchedules(sc);
      fetchAndShowLog();
      if (ls.status !== 'running') { stopLoopPoll(); loadData(); loadSchedules(); }
    } catch {}
  }, 4000);
}

function stopLoopPoll() {
  if (loopPollInterval) { clearInterval(loopPollInterval); loopPollInterval = null; }
}

function renderLoopStatus(d) {
  const bar     = document.getElementById('loop-status-bar');
  const pill    = document.getElementById('loop-pill');
  const stopBtn = document.getElementById('btn-stop-loop');
  const nowBtn  = document.getElementById('btn-loop-now');
  if (!bar) return;

  const status    = d?.status || 'idle';
  const isRunning = status === 'running';

  if (stopBtn) stopBtn.style.display = isRunning ? '' : 'none';
  if (nowBtn)  nowBtn.disabled = isRunning;

  if (!d?.updated || status === 'idle') {
    bar.style.display = 'none';
    if (pill) pill.style.display = 'none';
    return;
  }

  bar.style.display = '';
  if (pill) pill.style.display = '';

  if (isRunning) {
    if (pill) { pill.className = 'loop-pill running'; pill.textContent = 'RUNNING'; }
    bar.className = 'loop-status-bar running';
    Object.assign(bar.style, {display:'', alignItems:'', gap:''});
    bar.textContent = '● Loop running…';
  } else if (status === 'completed' || status === 'done') {
    if (pill) { pill.className = 'loop-pill'; pill.style.background='rgba(52,211,153,.15)'; pill.style.color='var(--green)'; pill.textContent='DONE'; }
    bar.className = 'loop-status-bar done';
    bar.textContent = '✓ Loop finished';
  } else if (status === 'error') {
    if (pill) { pill.className = 'loop-pill'; pill.style.background='rgba(239,68,68,.15)'; pill.style.color='#f87171'; pill.textContent='ERROR'; }
    bar.className = 'loop-status-bar error';
    bar.innerHTML = `<span>⚠ Loop error: ${esc(d.error || 'unknown')}</span>`
      + `<button onclick="dismissLoopError()" style="margin-left:auto;padding:1px 8px;font-size:10px;border-radius:4px;border:1px solid rgba(239,68,68,.4);background:rgba(239,68,68,.1);color:#f87171;cursor:pointer">✕ Dismiss</button>`;
    Object.assign(bar.style, {display:'flex', alignItems:'center', gap:'8px'});
  } else if (status === 'limit_reached') {
    if (pill) { pill.className = 'loop-pill'; pill.style.background='rgba(245,158,11,.15)'; pill.style.color='var(--yellow)'; pill.textContent='LIMIT'; }
    bar.className = 'loop-status-bar stopped';
    bar.innerHTML = `<span>⏸ Usage limit reached — see schedule entry below to resume</span>`
      + `<button onclick="dismissLoopError()" style="margin-left:auto;padding:1px 8px;font-size:10px;border-radius:4px;border:1px solid rgba(100,116,139,.4);background:transparent;color:var(--muted);cursor:pointer">✕ Dismiss</button>`;
    Object.assign(bar.style, {display:'flex', alignItems:'center', gap:'8px'});
  } else {
    if (pill) { pill.className = 'loop-pill'; pill.style.background=''; pill.style.color='var(--muted)'; pill.textContent=status.toUpperCase(); }
    bar.className = 'loop-status-bar stopped';
    Object.assign(bar.style, {display:'', alignItems:'', gap:''});
    bar.textContent = `Loop ${status}`;
  }
}

async function dismissLoopError() {
  try {
    await fetch(SERVER + '/dismiss-loop-error', { method: 'POST' });
    await loadLoopStatus();
  } catch {}
}

async function stopLoop() {
  try {
    await fetch(SERVER+'/stop-loop', {method:'POST'});
    // Don't call stopLoopPoll() — let polling detect the status change naturally
    await Promise.all([loadLoopStatus(), loadSchedules()]);
  } catch {}
}

// ── Schedule list rendering ───────────────────────────────────────────────────

function renderSchedules(d) {
  const el = document.getElementById('sched-list');
  if (!el) return;
  const schedules = (d?.schedules || []).filter(s => s.status !== 'completed');
  if (!schedules.length) {
    el.innerHTML = '<div style="font-size:12px;color:var(--muted);padding:4px 0">No active schedules.</div>';
    return;
  }
  el.innerHTML = schedules.map(s => schedCardHtml(s)).join('');
}

function schedCardHtml(s) {
  const status    = s.status || 'active';
  const cardCls   = status === 'running' ? ' running' : status === 'limit_reached' ? ' limit-reached' : status === 'error' ? ' error' : '';
  const statusCls = {active:'scs-active', running:'scs-running', limit_reached:'scs-limit', error:'scs-error'}[status] || 'scs-done';
  const statusTxt = status === 'limit_reached' ? 'LIMIT' : status.toUpperCase();

  const localTime = _utcToLocal(s.hour_utc ?? 0, s.minute_utc ?? 0);
  const h   = String(localTime.h).padStart(2,'0');
  const m   = String(localTime.m).padStart(2,'0');
  const tz  = _tzAbbr();
  const pat = s.repeat_pattern || 'daily';
  const wd  = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][s.weekday ?? 0];
  const patLbl = pat === 'weekdays' ? 'Weekdays' : pat === 'weekends' ? 'Weekends' : pat === 'weekly' ? `Weekly (${wd})` : 'Daily';
  const repLbl = s.repeat === -1 ? '∞ repeat' : s.repeat === 0 ? 'once' : `${s.runs_remaining} runs left`;
  const isImmediate = !!s.is_immediate;
  const timing = isImmediate
    ? (status === 'running' ? '▶ Running now' : 'Ran immediately')
    : (status === 'active' || status === 'running') ? `${patLbl} @ ${h}:${m} ${tz} · ${repLbl}` : patLbl;

  const sess = s.settings?.session_threshold ?? '—';
  const week = s.settings?.weekly_threshold  ?? '—';
  const ctx  = s.settings?.context_threshold ?? '—';
  const ar   = s.allow_reschedule ? ' · auto-reschedule on' : '';

  const searches   = (s.settings?.nextLoop_prompt_dynamic || []);
  const searchHtml = searches.length
    ? searches.slice(0,5).map(p => `<span>${esc(p)}</span>`).join('<br>') + (searches.length > 5 ? `<br><span style="color:var(--muted)">…+${searches.length-5} more</span>` : '')
    : '<span style="color:var(--muted)">No searches</span>';

  const lastRun = s.last_run ? `Last: ${_fmtUtcIso(s.last_run)}` : 'Never run';
  const nextRun = s.next_run && status === 'active' ? ` · Next: ${_fmtUtcIso(s.next_run)}` : '';

  const rem = s._remaining_count || 0;
  const sid = esc(s.id);
  let remainingHtml = '';
  if (status === 'limit_reached' && rem > 0) {
    const remaining = s.remaining_loop_prompts || [];
    remainingHtml = `<div class="sched-remaining-alert">
      ⚠ ${rem} search${rem>1?'es':''} not completed:
      <div style="margin-top:4px;opacity:.85">${remaining.slice(0,3).map(p=>esc(p)).join('<br>')}${rem>3?`<br>…+${rem-3} more`:''}</div>
    </div>`;
  }

  let actions = '';
  if (status === 'active') {
    actions = `<button class="filter-btn" style="font-size:10px;padding:2px 8px" onclick="runScheduleNow('${sid}')">▶ Now</button>
               <button class="btn-reject" onclick="cancelSchedule('${sid}')">✕</button>`;
  } else if (status === 'running') {
    actions = `<button class="btn-abort" style="font-size:10px;padding:2px 8px" onclick="stopLoop()">⏹ Stop</button>`;
  } else if (status === 'limit_reached') {
    actions = `${rem > 0 ? `<button class="btn-approve" style="font-size:10px;padding:3px 10px" onclick="resumeSchedule('${sid}')">↩ Resume</button>` : ''}
               <button class="filter-btn" style="font-size:10px;padding:2px 8px" onclick="returnToQueue('${sid}')">📋 Return to Queue</button>
               <button class="btn-reject" onclick="cancelSchedule('${sid}')">✕ Dismiss</button>`;
  } else if (status === 'error') {
    actions = `<button class="btn-reject" onclick="cancelSchedule('${sid}')">✕ Dismiss</button>`;
  }

  return `<div class="sched-card${cardCls}">
    <div class="sched-card-hdr">
      <span class="sched-card-title">${timing}</span>
      <span class="sched-card-status ${statusCls}">${statusTxt}</span>
    </div>
    <div class="sched-card-meta">Session ≤${sess}% · Weekly ≤${week}% · Context ≤${ctx}%${ar}</div>
    <div class="sched-searches"><strong>${searches.length} search${searches.length!==1?'es':''}</strong><br>${searchHtml}</div>
    ${remainingHtml}
    <div class="sched-card-meta">${lastRun}${nextRun}</div>
    <div class="sched-card-actions">${actions}</div>
  </div>`;
}

async function cancelSchedule(id) {
  if (!confirm('Cancel this scheduled loop?')) return;
  try {
    await fetch(SERVER+'/cancel-schedule', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({id}) });
    await loadSchedules();
  } catch {}
}

async function runScheduleNow(id) {
  try {
    const r = await fetch(SERVER+'/run-schedule-now', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({id}) });
    const d = await r.json();
    if (r.status === 202) { startLoopPoll(); await loadLoopStatus(); }
    else if (r.status === 409) { alert('A loop is already running.'); }
    else { alert('Error: ' + (d.error || r.status)); }
  } catch(e) { alert('Could not reach server: ' + e.message); }
}

// ── Tab init ──────────────────────────────────────────────────────────────────

(function initRunTabDefaults() {
  const badge = document.getElementById('sched-tz-badge');
  if (badge) badge.textContent = _tzAbbr();
  setSchedPattern(schedPattern); // enforce initial weekday-selector hidden state
})();
