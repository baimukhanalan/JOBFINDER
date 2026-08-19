// pm2 process manager for the local JobFinder admin stack (this Mac).
//   pm2 start ecosystem.config.js   # start both
//   pm2 save                        # remember them across restarts
//   pm2 logs jobfinder-bot          # tail the bot
//   pm2 restart jobfinder-bot       # after code/.env changes
//
// NOTE: the bot's "Open in browser to submit" launches a VISIBLE Chromium window.
// A GUI window can only appear while you are logged into the Mac desktop. pm2 keeps
// the bot/dashboard alive headlessly forever, but the submit window shows only when
// you're logged in (a boot-time daemon before login cannot pop a window).
const ROOT = "/Users/alanbaimukhan/Documents/JOBFINDER";

module.exports = {
  apps: [
    {
      name: "jobfinder-dash",
      cwd: ROOT,
      script: ".venv/bin/uvicorn",
      args: "backend.dashboard_app:app --host 127.0.0.1 --port 8089 --log-level warning",
      interpreter: "none",
      env: { PYTHONPATH: "." },
      autorestart: true,
      max_restarts: 20,
    },
    {
      name: "jobfinder-bot",
      cwd: ROOT,
      script: ".venv/bin/python",
      args: "-u -m backend.tools.apply_bot",
      interpreter: "none",
      env: { PYTHONPATH: "." },
      autorestart: true,
      max_restarts: 20,
    },
  ],
};
