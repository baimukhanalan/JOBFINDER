"""Optional offline STT bridge to the runtime already bundled with Natively."""
from __future__ import annotations

import asyncio
import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from qa_bot.audio.service import wav_signal_metrics


@dataclass(frozen=True)
class NativelyLocalSTT:
    project_root: Path
    app_path: Path = Path("/Applications/Natively.app")
    model: str = "distil-whisper/distil-small.en"

    def __post_init__(self) -> None:
        if not self.model or "/" not in self.model:
            raise ValueError("invalid local STT model")

    @property
    def electron(self) -> Path:
        return self.app_path / "Contents/MacOS/Natively"

    @property
    def transformers_root(self) -> Path:
        return (self.app_path / "Contents/Resources/app.asar.unpacked/node_modules"
                / "@huggingface/transformers")

    @property
    def driver(self) -> Path:
        return self.project_root / "tools/natively_local_stt.mjs"

    @property
    def cache_dir(self) -> Path:
        return self.project_root / "runs/models/transformers"

    def command(self, wav_path: Path) -> tuple[str, ...]:
        paths = (self.electron, self.transformers_root, self.driver, wav_path)
        if any(not path.exists() for path in paths):
            raise FileNotFoundError("Natively runtime, driver, or WAV input is missing")
        return (str(self.electron), str(self.driver), str(wav_path), str(self.cache_dir),
                str(self.transformers_root), self.model)

    async def transcribe_wav(self, wav_path: Path, *, timeout: float = 120) -> str:
        metrics = wav_signal_metrics(wav_path.read_bytes())
        if metrics["peak"] < 0.01 or metrics["rms"] < 0.002 or metrics["active_fraction"] < 0.01:
            raise ValueError("local STT input is silent or too weak")
        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"
        process = await asyncio.create_subprocess_exec(
            *self.command(wav_path), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise TimeoutError("local STT timed out") from None
        records = []
        for line in stdout.decode("utf-8", "replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("status") in ("ok", "error"):
                records.append(record)
        if process.returncode or not records or records[-1].get("status") != "ok":
            raise RuntimeError((records[-1].get("error") if records else None)
                               or "local STT failed")
        text = str(records[-1].get("text") or "").strip()
        if not text:
            raise RuntimeError("local STT returned empty text")
        return text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline English STT via Natively's local runtime")
    parser.add_argument("wav", type=Path, help="mono PCM16 16 kHz WAV")
    parser.add_argument("--project-root", type=Path,
                        default=Path(__file__).resolve().parents[4])
    parser.add_argument("--app-path", type=Path, default=Path("/Applications/Natively.app"))
    parser.add_argument("--model", default="distil-whisper/distil-small.en")
    parser.add_argument("--timeout", type=float, default=120)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        text = asyncio.run(NativelyLocalSTT(
            args.project_root.resolve(), app_path=args.app_path, model=args.model,
        ).transcribe_wav(args.wav.resolve(), timeout=args.timeout))
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)[:180]}))
        return 2
    print(json.dumps({"status": "ok", "text": text, "model": args.model}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
