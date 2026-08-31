// Relay the Alt+Shift+F hotkey to the active tab's content script.
chrome.commands.onCommand.addListener((cmd) => {
  if (cmd !== "tp-fill") return;
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) chrome.tabs.sendMessage(tabs[0].id, { type: "fill" }, () => void chrome.runtime.lastError);
  });
});
