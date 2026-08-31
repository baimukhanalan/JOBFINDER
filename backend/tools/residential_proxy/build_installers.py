#!/usr/bin/env python3
"""Fill the installer templates with the tunnel private key + settings -> dist/ (gitignored).

The templates are committed with __PLACEHOLDER__s (no secret); the filled installers carry the
locked-down tunnel key and are handed to the owner. Regenerate after rotating the key.
"""
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
KEY = (HERE / "dist" / "tunnel_key").read_text()

REPL = {
    "__HOST__": "proxy.systeam.kz",
    "__RPORT__": "8120",
    "__LP__": "8899",
    "__GOSTVER__": "2.11.5",
    "__PRIVKEY__": KEY.strip("\n"),
}

for tpl in ("install-proxy-windows.ps1.template", "install-proxy-mac.sh.template"):
    text = (HERE / tpl).read_text()
    for k, v in REPL.items():
        text = text.replace(k, v)
    dest = HERE / "dist" / tpl.replace(".template", "")
    dest.write_text(text)
    print("wrote", dest)
