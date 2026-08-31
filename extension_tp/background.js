// Relay the Alt+Shift+F hotkey to the active tab's content script.
chrome.commands.onCommand.addListener((cmd) => {
  if (cmd !== "tp-fill") return;
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) chrome.tabs.sendMessage(tabs[0].id, { type: "fill" }, () => void chrome.runtime.lastError);
  });
});

// The content script runs on icims.com and can't fetch our server cross-origin, so the background
// (which has host_permissions for jobs.systeam.kz) proxies two reads for it:
//   - tp_code:   the account-verification code the server received in the persona's @takhet.com inbox
//   - tp_resume: the persona's résumé PDF (returned base64 so the content script can build a File)
// Both self-authenticate with the shared X-Assist-Token (must match backend/.assist_token).
const SERVER = "https://jobs.systeam.kz";
const ASSIST_TOKEN = "2UzSKnxqRZY2VXbQEDRciRCooZ6TJjPa";

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg) return;
  if (msg.type === "tp_code") {
    fetch(`${SERVER}/tp_code?mailbox=${encodeURIComponent(msg.mailbox || "")}&since=${msg.since || 0}`,
          { headers: { "X-Assist-Token": ASSIST_TOKEN } })
      .then((r) => r.json())
      .then((j) => sendResponse({ code: (j && j.code) || null }))
      .catch(() => sendResponse({ code: null }));
    return true;   // async response
  }
  if (msg.type === "tp_resume") {
    fetch(`${SERVER}/tp_resume?mailbox=${encodeURIComponent(msg.mailbox || "")}`,
          { headers: { "X-Assist-Token": ASSIST_TOKEN } })
      .then((r) => (r.ok ? r.arrayBuffer() : Promise.reject(new Error("no resume"))))
      .then((buf) => {
        const bytes = new Uint8Array(buf);
        let bin = "";
        for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
        sendResponse({ b64: btoa(bin) });
      })
      .catch(() => sendResponse({ b64: null }));
    return true;   // async response
  }
});
