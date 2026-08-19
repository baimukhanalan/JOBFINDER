// Apply Assist background service worker.
// 1) Relays the keyboard command to the active tab (Alt+Shift+F = local fill only).
// 2) Proxies server calls (fetch from the SW bypasses page CORS via host_permissions):
//    - "assist"      POST /assist       — smart fill: closed screeners + open questions
//    - "draft"       POST /draft        — legacy open-questions-only fallback
//    - "getIdentity" GET  /profile_form — the selected profile's identity fields,
//                                          cached in chrome.storage.local for 24h
//    - "jobPack"     GET  /job_pack     — one job's prefill pack for the 1-click fill
//    - "resumeFile"  GET  /resume_file  — the job's tailored résumé PDF (as base64)
//    - "markExt"     POST /mark_ext     — flip the dashboard card after a real submit
// The active profile id lives in chrome.storage.sync under "aa_profile" (popup sets it).
// The server base can be overridden via chrome.storage.local "aa_base" (E2E points it
// at a local server); it is read on EVERY call, never cached in a const.
const DEFAULT_SERVER = "https://jobfinder.systeam.kz";
const ASSIST_TOKEN = "REPLACE_WITH_BACKEND_ASSIST_TOKEN";
const IDENTITY_TTL = 24 * 60 * 60 * 1000; // 24h

chrome.commands.onCommand.addListener((cmd) => {
  if (cmd !== "fill-form") return;
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) chrome.tabs.sendMessage(tabs[0].id, { type: "fill" });
  });
});

function getServer() {
  return new Promise((res) => {
    try {
      chrome.storage.local.get({ aa_base: DEFAULT_SERVER }, (v) => {
        const base = ((v && v.aa_base) || DEFAULT_SERVER).trim().replace(/\/+$/, "");
        res(base || DEFAULT_SERVER);
      });
    } catch (e) { res(DEFAULT_SERVER); }
  });
}

function getProfileId() {
  return new Promise((res) => {
    try {
      chrome.storage.sync.get({ aa_profile: "michael" }, (v) =>
        res(((v && v.aa_profile) || "michael").trim() || "michael"));
    } catch (e) { res("michael"); }
  });
}

async function post(path, body) {
  const server = await getServer();
  const r = await fetch(server + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Assist-Token": ASSIST_TOKEN },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

async function get(path, timeoutMs) {
  const server = await getServer();
  return fetch(server + path, {
    headers: { "X-Assist-Token": ASSIST_TOKEN },
    signal: AbortSignal.timeout(timeoutMs || 15000),
  });
}

// arrayBuffer -> base64, chunked: String.fromCharCode over a whole PDF in one call
// would overflow the engine's argument limit on files past ~100KB.
function bufToB64(buf) {
  const bytes = new Uint8Array(buf);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(bin);
}

async function identityData() {
  const pid = await getProfileId();
  const key = "aa_identity_" + pid;
  const st = await new Promise((res) => {
    try { chrome.storage.local.get(key, (v) => res(v || {})); } catch (e) { res({}); }
  });
  const hit = st[key];
  if (hit && hit.ts && Date.now() - hit.ts < IDENTITY_TTL && hit.data) {
    return { profile: pid, identity: hit.data };
  }
  const r = await get(`/profile_form?profile=${encodeURIComponent(pid)}`, 6000); // fill must never hang on a slow server
  if (!r.ok) throw new Error("HTTP " + r.status);
  const data = await r.json();
  try { chrome.storage.local.set({ [key]: { ts: Date.now(), data } }); } catch (e) { /* cache only */ }
  return { profile: pid, identity: data };
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (!msg) return;
  // Every handler runs inside this guard: any throw — synchronous (invalidated
  // context, storage API missing) or async (HTTP, abort) — still reply()s,
  // otherwise content.js would silently wait out its own timeout.
  const handle = (fn) => {
    Promise.resolve()
      .then(fn)
      .then((d) => reply({ ok: true, ...(d || {}) }))
      .catch((e) => reply({ ok: false, error: String(e) }));
    return true; // async reply
  };

  if (msg.type === "draft") {
    return handle(async () => post("/draft", {
      profile: await getProfileId(),
      questions: msg.questions || [],
      company: msg.company || "",
      job_title: msg.job_title || "",
    }));
  }
  if (msg.type === "assist") {
    return handle(async () => post("/assist", {
      profile: await getProfileId(),
      company: msg.company || "",
      job_title: msg.job_title || "",
      closed: msg.closed || [],
      open: msg.open || [],
    }));
  }
  if (msg.type === "getIdentity") {
    return handle(identityData);
  }
  if (msg.type === "jobPack") {
    return handle(async () => {
      const r = await get(`/job_pack?profile=${encodeURIComponent(msg.profile || "")}` +
        `&jid=${encodeURIComponent(msg.jid || "")}`);
      if (!r.ok) throw new Error("HTTP " + r.status);
      return { pack: await r.json() };
    });
  }
  if (msg.type === "resumeFile") {
    return handle(async () => {
      const r = await get(`/resume_file?profile=${encodeURIComponent(msg.profile || "")}` +
        `&jid=${encodeURIComponent(msg.jid || "")}`, 20000);
      if (!r.ok) throw new Error("HTTP " + r.status);
      return { b64: bufToB64(await r.arrayBuffer()) };
    });
  }
  if (msg.type === "markExt") {
    return handle(() => post("/mark_ext", {
      profile: msg.profile || "",
      jid: msg.jid || "",
      to: msg.to || "",
    }));
  }
});
