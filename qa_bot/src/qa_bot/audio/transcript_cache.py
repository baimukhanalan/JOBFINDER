"""Exact original-audio transcript reuse with bounded local STT concurrency."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import fcntl
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@lru_cache(maxsize=128)
def _file_hash(path, size, mtime_ns, ctime_ns):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def natively_model_identity(stt) -> str:
    """Bind reuse to model weights/config, driver and bundled runtime version."""
    model_root = (stt.cache_dir / stt.model).resolve()
    files = sorted(p for p in model_root.rglob('*') if p.is_file())
    if not files or not any(p.suffix == '.onnx' for p in files):
        raise ValueError('local STT model must be installed before shared reuse')
    fingerprints = []
    for path in [stt.driver, stt.transformers_root / 'package.json', *files]:
        stat = path.stat()
        label = str(path.relative_to(model_root)) if path.is_relative_to(model_root) else path.name
        fingerprints.append((label, _file_hash(str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)))
    return _json({'engine': 'natively-local-stt', 'model': stt.model,
                  'files': fingerprints, 'identity_version': 1})


@asynccontextmanager
async def _lock(path, timeout):
    with path.open('a+b') as stream:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError('shared transcript worker wait timed out')
                await asyncio.sleep(.025)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class CachedTranscript:
    text: str
    source: str
    audio_sha256: str
    model_sha256: str
    text_sha256: str


class TranscriptCache:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.locks = self.directory / '.locks'
        self.locks.mkdir(exist_ok=True)

    @staticmethod
    def _identity(audio, model_identity):
        if not isinstance(audio, bytes) or not audio:
            raise ValueError('original audio bytes required')
        if not isinstance(model_identity, str) or not model_identity.strip():
            raise ValueError('explicit STT model identity required')
        audio_sha = hashlib.sha256(audio).hexdigest()
        model_sha = hashlib.sha256(model_identity.encode()).hexdigest()
        key = hashlib.sha256(_json({'version': 1, 'audio': audio_sha, 'model': model_sha}).encode()).hexdigest()
        return key, audio_sha, model_sha

    def _lookup(self, path, audio_sha, model_sha):
        if not path.exists():
            return None
        try:
            row = json.loads(path.read_text())
            text = row['text']
            if (row['version'] != 1 or row['audio_sha256'] != audio_sha
                    or row['model_sha256'] != model_sha or not isinstance(text, str) or not text.strip()
                    or hashlib.sha256(text.encode()).hexdigest() != row['text_sha256']):
                raise ValueError('mismatch')
        except (ValueError, KeyError, TypeError):
            raise ValueError('shared transcript integrity failure') from None
        return CachedTranscript(text, 'replay', audio_sha, model_sha, row['text_sha256'])

    async def get_or_transcribe(self, audio, *, model_identity, transcribe, source=None, timeout=180):
        key, audio_sha, model_sha = self._identity(audio, model_identity)
        path = self.directory / (key + '.json')
        found = self._lookup(path, audio_sha, model_sha)
        if found:
            return found
        async with _lock(self.locks / (key + '.lock'), timeout):
            found = self._lookup(path, audio_sha, model_sha)
            if found:
                return found
            # Distinct new clips also serialize: ten sessions cannot load ten models.
            async with _lock(self.locks / 'stt-worker.lock', timeout):
                text = await transcribe()
            if not isinstance(text, str) or not text.strip():
                raise ValueError('local STT returned empty text')
            text = text.strip()
            digest = hashlib.sha256(text.encode()).hexdigest()
            row = {'version': 1, 'audio_sha256': audio_sha, 'model_sha256': model_sha,
                   'text': text, 'text_sha256': digest, 'created_at': time.time(),
                   'source': source or {}}
            with tempfile.NamedTemporaryFile(mode='w', dir=self.directory, delete=False) as stream:
                staged = Path(stream.name)
                stream.write(_json(row))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.replace(staged, path)
            finally:
                staged.unlink(missing_ok=True)
            return CachedTranscript(text, 'transcribed', audio_sha, model_sha, digest)
