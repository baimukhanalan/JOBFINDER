// Quick Apply — popup controller

let state = {
  running: false,
  currentJob: null,
  serverUrl: 'http://173.249.18.153:8000',
  platform: '',
  lastJobId: 0,
  stats: { submitted: 0, skipped: 0, expired: 0, error: 0 },
};

// DOM refs
const $ = (id) => document.getElementById(id);

// Load saved state
chrome.storage.local.get(['qaState'], (result) => {
  if (result.qaState) {
    Object.assign(state, result.qaState);
    $('serverUrl').value = state.serverUrl;
    $('platform').value = state.platform;
    $('startFrom').value = state.lastJobId;
    updateStats();
  }
});

function saveState() {
  chrome.storage.local.set({ qaState: state });
}

function updateStats() {
  $('statSent').textContent = state.stats.submitted;
  $('statSkip').textContent = `${state.stats.skipped + state.stats.expired} skip`;
}

function setStatus(text, cls = 'loading') {
  $('statusMsg').textContent = text;
  $('statusMsg').className = `status ${cls}`;
}

function showButtons(fill = false, actions = false) {
  $('btnFill').style.display = fill ? 'block' : 'none';
  $('btnSubmitted').style.display = actions ? 'block' : 'none';
  $('btnSkip').style.display = actions ? 'block' : 'none';
}

async function api(path) {
  const resp = await fetch(`${state.serverUrl}/api/ext${path}`);
  return resp.json();
}

async function apiPost(path, body) {
  const resp = await fetch(`${state.serverUrl}/api/ext${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return resp.json();
}

// Send message to content script in active tab
function sendToTab(msg) {
  return new Promise((resolve) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        chrome.tabs.sendMessage(tabs[0].id, msg, resolve);
      } else {
        resolve(null);
      }
    });
  });
}

// ===== Flow =====

$('btnStart').addEventListener('click', async () => {
  state.serverUrl = $('serverUrl').value.replace(/\/$/, '');
  state.platform = $('platform').value;
  state.lastJobId = parseInt($('startFrom').value) || 0;
  state.running = true;
  state.stats = { submitted: 0, skipped: 0, expired: 0, error: 0 };
  saveState();

  $('idleView').style.display = 'none';
  $('runView').style.display = 'block';

  await loadNextJob();
});

$('btnStop').addEventListener('click', () => {
  state.running = false;
  saveState();
  $('idleView').style.display = 'block';
  $('runView').style.display = 'none';
  setStatus('Stopped', 'done');
});

$('btnFill').addEventListener('click', async () => {
  setStatus('Auto-filling...', 'loading');
  showButtons(false, false);

  // Get profile and answers from server
  const [profile, answersResp] = await Promise.all([
    api('/profile'),
    api('/answers'),
  ]);

  // Send fill command to content script
  const result = await sendToTab({
    action: 'fill',
    profile,
    answers: answersResp.answers || {},
  });

  if (result && result.unanswered && result.unanswered.length > 0) {
    showQuestions(result.unanswered);
    setStatus(`Filled ${result.filled} fields, ${result.unanswered.length} need answers`, 'ready');
  } else {
    setStatus(`Filled ${result?.filled || 0} fields — review and submit!`, 'ready');
  }
  showButtons(true, true);
});

$('btnSubmitted').addEventListener('click', async () => {
  // Save any answered questions
  const questions = document.querySelectorAll('.q-item [data-question]');
  for (const el of questions) {
    const val = el.value.trim();
    if (val) {
      await apiPost('/answers', {
        question: el.dataset.question,
        answer: val,
        domain: new URL(state.currentJob.apply_url).hostname,
      });
      // Also fill on the page
      await sendToTab({
        action: 'fill_one',
        selector: el.dataset.selector,
        value: val,
        type: el.dataset.fieldtype,
      });
    }
  }

  await apiPost('/result', {
    job_id: state.currentJob.job_id,
    status: 'submitted',
  });
  state.stats.submitted++;
  state.lastJobId = state.currentJob.job_id;
  updateStats();
  saveState();

  await loadNextJob();
});

$('btnSkip').addEventListener('click', async () => {
  await apiPost('/result', {
    job_id: state.currentJob.job_id,
    status: 'skipped',
  });
  state.stats.skipped++;
  state.lastJobId = state.currentJob.job_id;
  updateStats();
  saveState();

  await loadNextJob();
});

async function loadNextJob() {
  setStatus('Loading next job...', 'loading');
  showButtons(false, false);
  $('questionsBox').innerHTML = '';

  try {
    const platform = state.platform ? `&platform=${state.platform}` : '';
    const job = await api(`/next?after=${state.lastJobId}${platform}`);

    if (job.done) {
      setStatus('All done! No more jobs.', 'done');
      $('jobInfo').style.display = 'none';
      return;
    }

    state.currentJob = job;

    // Show job info
    $('jobInfo').style.display = 'block';
    $('jobTitle').textContent = job.title;
    $('jobCompany').textContent = job.company;
    $('jobId').textContent = `#${job.job_id}`;

    // Navigate to apply URL
    let url = job.apply_url;
    if (url.includes('lever.co') && !url.includes('/apply')) {
      url = url.replace(/\/?$/, '/apply');
    }

    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      if (tabs[0]) {
        chrome.tabs.update(tabs[0].id, { url });
      }
    });

    setStatus('Page loading... Click "Auto-Fill" when ready', 'loading');
    // Show fill button after a delay for page to load
    setTimeout(() => {
      showButtons(true, false);
      setStatus('Click "Auto-Fill Form" to fill fields', 'ready');
    }, 3000);

  } catch (err) {
    setStatus(`Error: ${err.message}`, 'error');
  }
}

function showQuestions(unanswered) {
  const box = $('questionsBox');
  box.innerHTML = '';
  for (const q of unanswered) {
    const div = document.createElement('div');
    div.className = 'q-item';

    let input;
    if (q.options && q.options.length > 0) {
      const opts = q.options.map(o => `<option value="${esc(o)}">${esc(o)}</option>`).join('');
      input = `<select data-question="${esc(q.label)}" data-selector="${esc(q.selector)}" data-fieldtype="${q.type}">
        <option value="">— Select —</option>${opts}</select>`;
    } else if (q.label.length > 80) {
      input = `<textarea data-question="${esc(q.label)}" data-selector="${esc(q.selector)}" data-fieldtype="${q.type}" placeholder="Your answer..."></textarea>`;
    } else {
      input = `<input type="text" data-question="${esc(q.label)}" data-selector="${esc(q.selector)}" data-fieldtype="${q.type}" placeholder="Your answer...">`;
    }

    div.innerHTML = `<label>${esc(q.label)}</label>${input}`;
    box.appendChild(div);
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML.replace(/"/g, '&quot;');
}
