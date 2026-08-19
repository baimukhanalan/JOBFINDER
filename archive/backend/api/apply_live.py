"""Apply queue page — simple job list with Open/Done/Skip buttons.

User installs Simplify (or similar) extension for auto-fill.
This page just manages the queue.
"""
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

live_router = APIRouter()


@live_router.get("/apply")
async def apply_page():
    return HTMLResponse(PAGE_HTML)


PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Quick Apply Queue</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; }

.top-bar { position: fixed; top: 0; left: 0; right: 0; z-index: 100; background: #1e293b; border-bottom: 1px solid #334155; padding: 10px 20px; display: flex; align-items: center; justify-content: space-between; }
.top-bar h1 { font-size: 16px; color: #38bdf8; }
.stats { display: flex; gap: 10px; font-size: 13px; }
.stat { padding: 4px 12px; border-radius: 6px; background: #0f172a; }
.stat b { color: #22c55e; }
.stat.total b { color: #38bdf8; }

.container { max-width: 800px; margin: 0 auto; padding: 70px 16px 20px; }

/* Current job card */
.current { background: #1e293b; border-radius: 12px; padding: 24px; margin-bottom: 16px; border: 2px solid #334155; }
.current.active { border-color: #22c55e; }
.badge { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 4px; background: #334155; color: #94a3b8; margin-bottom: 8px; }
.badge.open { background: #14532d; color: #86efac; }
.job-title { font-size: 20px; font-weight: 700; color: #f1f5f9; margin-bottom: 4px; }
.job-company { font-size: 15px; color: #94a3b8; margin-bottom: 8px; }
.job-meta { font-size: 12px; color: #64748b; margin-bottom: 12px; }
.job-desc { font-size: 13px; color: #94a3b8; line-height: 1.5; margin-bottom: 16px; max-height: 80px; overflow: hidden; transition: max-height 0.3s; }
.job-desc.expanded { max-height: 500px; }
.toggle-desc { font-size: 12px; color: #38bdf8; cursor: pointer; margin-bottom: 16px; display: inline-block; }

.actions { display: flex; gap: 10px; }
.btn { padding: 12px 24px; font-size: 15px; font-weight: 600; border: none; border-radius: 10px; cursor: pointer; transition: all 0.12s; }
.btn:active { transform: scale(0.96); }
.btn:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-open { background: #3b82f6; color: white; flex: 2; }
.btn-open:hover:not(:disabled) { background: #2563eb; }
.btn-done { background: #22c55e; color: white; flex: 2; }
.btn-done:hover:not(:disabled) { background: #16a34a; }
.btn-skip { background: #475569; color: #e2e8f0; flex: 1; }
.btn-skip:hover:not(:disabled) { background: #64748b; }
.btn-dead { background: #7f1d1d; color: #fca5a5; flex: 1; font-size: 13px; padding: 8px; }
.btn-dead:hover:not(:disabled) { background: #991b1b; }

/* Keyboard hint */
.keys { text-align: center; margin-top: 10px; font-size: 11px; color: #475569; }
.keys kbd { background: #334155; padding: 2px 6px; border-radius: 3px; color: #94a3b8; }

/* History log */
.history { margin-top: 20px; }
.history h3 { font-size: 13px; color: #64748b; margin-bottom: 8px; }
.log-item { display: flex; align-items: center; gap: 8px; padding: 6px 0; border-bottom: 1px solid #1e293b; font-size: 13px; }
.log-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.log-dot.done { background: #22c55e; }
.log-dot.skip { background: #f59e0b; }
.log-dot.expired { background: #64748b; }
.log-title { color: #94a3b8; flex: 1; }
.log-company { color: #64748b; }

/* Loading */
.loading { text-align: center; padding: 60px; }
.spinner { display: inline-block; width: 28px; height: 28px; border: 3px solid #334155; border-top-color: #38bdf8; border-radius: 50%; animation: spin 0.7s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* Empty */
.empty { text-align: center; padding: 80px 20px; }
.empty h2 { color: #22c55e; font-size: 22px; margin-bottom: 8px; }
.empty p { color: #64748b; }

/* Settings */
.settings { display: flex; gap: 10px; margin-bottom: 16px; align-items: center; }
.settings select, .settings input { padding: 8px 12px; border-radius: 8px; border: 1px solid #334155; background: #1e293b; color: #e2e8f0; font-size: 13px; }
.settings input { width: 100px; }
</style>
</head>
<body>

<div class="top-bar">
  <h1>⚡ Quick Apply</h1>
  <div class="stats">
    <span class="stat total"><b id="sTotal">-</b> jobs</span>
    <span class="stat"><b id="sDone">0</b> applied</span>
    <span class="stat" id="sSkipWrap">0 skipped</span>
  </div>
</div>

<div class="container">
  <div class="settings">
    <select id="platform">
      <option value="">All platforms</option>
      <option value="greenhouse">Greenhouse</option>
      <option value="lever">Lever</option>
      <option value="ashby">Ashby</option>
      <option value="icims">iCIMS</option>
      <option value="applytojob">ApplyToJob</option>
      <option value="smartrecruiters">SmartRecruiters</option>
    </select>
    <input type="number" id="startFrom" value="0" placeholder="From ID">
    <button class="btn btn-skip" onclick="loadNext()" style="padding:8px 16px; font-size:13px;">Reload</button>
  </div>

  <div id="loading" class="loading"><div class="spinner"></div></div>

  <div id="jobCard" class="current" style="display:none">
    <span class="badge" id="jobBadge">Loading</span>
    <div class="job-title" id="jobTitle"></div>
    <div class="job-company" id="jobCompany"></div>
    <div class="job-meta" id="jobMeta"></div>
    <div class="job-desc" id="jobDesc"></div>
    <span class="toggle-desc" onclick="toggleDesc()">Show more</span>
    <div class="actions">
      <button class="btn btn-open" id="btnOpen" onclick="openJob()">Open & Apply ↗</button>
      <button class="btn btn-done" id="btnDone" onclick="markDone()" style="display:none">✓ Applied</button>
      <button class="btn btn-skip" id="btnSkip" onclick="skipJob()">Skip →</button>
      <button class="btn btn-dead" id="btnDead" onclick="markDead()">Dead ✕</button>
    </div>
    <div class="keys">
      <kbd>O</kbd> open &nbsp; <kbd>D</kbd> applied &nbsp; <kbd>S</kbd> skip &nbsp; <kbd>X</kbd> dead link
    </div>
  </div>

  <div id="emptyState" class="empty" style="display:none">
    <h2>All done!</h2>
    <p>No more jobs in queue.</p>
  </div>

  <div class="history" id="historyBox" style="display:none">
    <h3>Recent</h3>
    <div id="historyList"></div>
  </div>
</div>

<script>
const API = '/api/ext';
let job = null;
let lastId = 0;
let stats = { done: 0, skipped: 0 };
let history = [];
let opened = false;

async function api(path) {
  const r = await fetch(API + path);
  return r.json();
}
async function post(path, body) {
  const r = await fetch(API + path, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
  return r.json();
}

async function loadNext() {
  const from = parseInt(document.getElementById('startFrom').value) || lastId;
  const platform = document.getElementById('platform').value;
  const pq = platform ? `&platform=${platform}` : '';

  document.getElementById('loading').style.display = 'block';
  document.getElementById('jobCard').style.display = 'none';
  document.getElementById('emptyState').style.display = 'none';
  opened = false;

  try {
    const data = await api(`/next?after=${from}${pq}`);

    if (data.done) {
      document.getElementById('loading').style.display = 'none';
      document.getElementById('emptyState').style.display = 'block';
      return;
    }

    job = data;
    lastId = job.job_id;

    document.getElementById('jobTitle').textContent = job.title;
    document.getElementById('jobCompany').textContent = job.company;
    document.getElementById('jobMeta').textContent = `#${job.job_id} • ${new URL(job.apply_url).hostname}`;
    document.getElementById('jobDesc').textContent = stripHtml(job.description || '');
    document.getElementById('jobBadge').textContent = new URL(job.apply_url).hostname.replace('www.','');
    document.getElementById('jobBadge').className = 'badge';

    document.getElementById('btnOpen').style.display = 'block';
    document.getElementById('btnDone').style.display = 'none';

    document.getElementById('loading').style.display = 'none';
    document.getElementById('jobCard').style.display = 'block';
    document.getElementById('jobCard').className = 'current';

  } catch(e) {
    document.getElementById('loading').style.display = 'none';
    alert('Error: ' + e.message);
  }
}

function openJob() {
  if (!job) return;
  let url = job.apply_url;
  if (url.includes('lever.co') && !url.includes('/apply')) {
    url = url.replace(/\\/?$/, '/apply');
  }
  window.open(url, '_blank');
  opened = true;

  document.getElementById('jobCard').className = 'current active';
  document.getElementById('jobBadge').textContent = 'OPENED';
  document.getElementById('jobBadge').className = 'badge open';
  document.getElementById('btnOpen').style.display = 'none';
  document.getElementById('btnDone').style.display = 'block';
}

async function markDone() {
  if (!job) return;
  await post('/result', { job_id: job.job_id, status: 'submitted' });
  stats.done++;
  addHistory(job, 'done');
  updateStats();
  loadNext();
}

async function skipJob() {
  if (!job) return;
  await post('/result', { job_id: job.job_id, status: 'skipped' });
  stats.skipped++;
  addHistory(job, 'skip');
  updateStats();
  loadNext();
}

async function markDead() {
  if (!job) return;
  await post('/result', { job_id: job.job_id, status: 'expired', error: 'dead_link' });
  stats.dead = (stats.dead || 0) + 1;
  addHistory(job, 'expired');
  updateStats();
  loadNext();
}

function addHistory(j, type) {
  history.unshift({ title: j.title, company: j.company, type });
  if (history.length > 20) history.pop();
  renderHistory();
}

function renderHistory() {
  if (history.length === 0) { document.getElementById('historyBox').style.display = 'none'; return; }
  document.getElementById('historyBox').style.display = 'block';
  document.getElementById('historyList').innerHTML = history.map(h =>
    `<div class="log-item"><span class="log-dot ${h.type}"></span><span class="log-title">${esc(h.title)}</span><span class="log-company">${esc(h.company)}</span></div>`
  ).join('');
}

function updateStats() {
  document.getElementById('sDone').textContent = stats.done;
  document.getElementById('sSkipWrap').textContent = stats.skipped + ' skipped';
}

function toggleDesc() {
  document.getElementById('jobDesc').classList.toggle('expanded');
}

function stripHtml(html) {
  const tmp = document.createElement('div');
  tmp.innerHTML = html;
  return tmp.textContent || tmp.innerText || '';
}

function esc(s) { const d = document.createElement('div'); d.textContent = s||''; return d.innerHTML; }

// Keyboard shortcuts
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
  if (e.key === 'o' || e.key === 'O' || e.key === 'ш' || e.key === 'Ш') openJob();
  if (e.key === 'd' || e.key === 'D' || e.key === 'в' || e.key === 'В') markDone();
  if (e.key === 's' || e.key === 'S' || e.key === 'ы' || e.key === 'Ы' || e.key === 'ArrowRight') skipJob();
  if (e.key === 'x' || e.key === 'X' || e.key === 'ч' || e.key === 'Ч') markDead();
});

// Load stats and first job
(async () => {
  const s = await api('/stats');
  document.getElementById('sTotal').textContent = s.total_jobs || 0;
  stats.done = s.submitted || 0;
  stats.skipped = s.skipped || 0;
  updateStats();
  loadNext();
})();
</script>
</body>
</html>
"""
