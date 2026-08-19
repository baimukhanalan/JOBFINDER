const resEl = document.getElementById("res");
const whoEl = document.getElementById("who");
const pidEl = document.getElementById("pid");

// Active profile id (chrome.storage.sync, shared with the background worker).
function showProfile() {
  chrome.storage.sync.get({ aa_profile: "michael" }, (v) => {
    const pid = ((v && v.aa_profile) || "michael").trim() || "michael";
    whoEl.textContent = "Profile: " + pid;
    pidEl.value = pid;
  });
}
showProfile();

// 1-click mode indicator: the dashboard's Apply link carries #aa=<profile>:<jid>.
chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
  const url = (tabs && tabs[0] && tabs[0].url) || "";
  const m = /#aa=([a-z0-9_-]+):([a-z0-9_-]+)/.exec(url);
  if (m) document.getElementById("oneclick").textContent = "1-click mode: " + m[2];
});

document.getElementById("savePid").addEventListener("click", () => {
  const pid = (pidEl.value || "").trim().toLowerCase() || "michael";
  chrome.storage.sync.set({ aa_profile: pid }, () => {
    showProfile();
    resEl.textContent = "Saved — using profile “" + pid + "”.";
  });
});

async function inject(tab) {
  // Make sure the content script is present (manifest may have injected it already).
  // allFrames stays true: Greenhouse and friends embed the form in an iframe; the
  // content script itself bails out of frames that hold no form.
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      files: ["profile.js", "content.js"],
    });
  } catch (e) { /* already injected via manifest — fine */ }
}

document.getElementById("fill").addEventListener("click", async () => {
  resEl.textContent = "Filling…";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) { resEl.textContent = "No active tab."; return; }
  await inject(tab);
  chrome.tabs.sendMessage(tab.id, { type: "fill" }, (r) => {
    if (chrome.runtime.lastError || !r) { resEl.textContent = "Filled. Review the page."; return; }
    resEl.textContent = `Filled ${r.filled} field(s)` + (r.needs ? ` · ${r.needs} need you` : "");
  });
});

document.getElementById("draft").addEventListener("click", async () => {
  resEl.textContent = "Smart filling…";
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab) { resEl.textContent = "No active tab."; return; }
  await inject(tab);
  chrome.tabs.sendMessage(tab.id, { type: "draft-fill" }, () => {
    if (chrome.runtime.lastError) { /* frame without the script — fine */ }
    resEl.textContent = "Smart fill running… watch the page for results.";
  });
});

chrome.runtime.onMessage.addListener((m) => {
  if (m && m.type === "fillResult") {
    resEl.textContent = `Filled ${m.filled} field(s)` + (m.needs ? ` · ${m.needs} need you` : "");
  }
});
