document.getElementById("fill").addEventListener("click", () => {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) chrome.tabs.sendMessage(tabs[0].id, { type: "fill" }, () => void chrome.runtime.lastError);
    window.close();
  });
});

// Ask every frame's content script to build + copy the DOM report (the form frame shows a
// selectable overlay so it can be copied by hand if the programmatic copy is blocked).
document.getElementById("report").addEventListener("click", () => {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    if (tabs[0]) chrome.tabs.sendMessage(tabs[0].id, { type: "report" }, () => void chrome.runtime.lastError);
    window.close();
  });
});
