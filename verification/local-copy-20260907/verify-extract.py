"""Restore an authenticated snapshot and verify every recorded entry."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tarfile


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


archive, destination = sys.argv[1:]
out = Path(destination)
if out.is_symlink() or (out.exists() and (not out.is_dir() or any(out.iterdir()))):
    raise SystemExit('Destination must be a new or empty directory')
out.mkdir(parents=True, exist_ok=True)
with tarfile.open(archive, 'r:gz') as tar:
    members = tar.getmembers()
    by_name = {m.name: m for m in members}
    if len(by_name) != len(members):
        raise SystemExit('Duplicate archive entries')
    manifest = json.load(tar.extractfile('__snapshot_manifest__.json'))
    entries = manifest['entries']
    expected = {'project/' + e['path'] for e in entries} | {'__snapshot_manifest__.json'}
    if set(by_name) != expected:
        raise SystemExit('Archive and manifest entries differ')
    paths = set()
    for entry in entries:
        name = entry['path']
        rel = PurePosixPath(name)
        if rel.is_absolute() or '..' in rel.parts or str(rel) != name or name in paths:
            raise SystemExit('Invalid archive path')
        paths.add(name)
        member = by_name['project/' + name]
        kind = entry['kind']
        if not ((kind == 'dir' and member.isdir()) or (kind == 'file' and member.isfile()) or (kind == 'symlink' and member.issym()) or (kind == 'fifo' and member.isfifo())):
            raise SystemExit('Unsupported or mismatched entry type')
        if kind == 'symlink' and member.linkname != entry['target']:
            raise SystemExit('Symlink target mismatch')
    kinds = {e['path']: e['kind'] for e in entries}
    for entry in entries:
        for parent in PurePosixPath(entry['path']).parents:
            if str(parent) != '.' and kinds.get(str(parent)) != 'dir':
                raise SystemExit('Invalid parent directory')
    # Never write through archive symlinks: create all symlinks last.
    for entry in sorted(entries, key=lambda e: (e['kind'] == 'symlink', len(PurePosixPath(e['path']).parts))):
        path = out / entry['path']
        member = by_name['project/' + entry['path']]
        if entry['kind'] == 'dir':
            path.mkdir(exist_ok=True)
        elif entry['kind'] == 'file':
            with tar.extractfile(member) as source, path.open('xb') as target:
                shutil.copyfileobj(source, target)
            os.chmod(path, entry['mode'])
            os.utime(path, ns=(entry['mtime_ns'], entry['mtime_ns']))
        elif entry['kind'] == 'fifo':
            os.mkfifo(path, entry['mode'])
            os.chmod(path, entry['mode'])
            os.utime(path, ns=(entry['mtime_ns'], entry['mtime_ns']))
        else:
            path.symlink_to(entry['target'])
            os.utime(path, ns=(entry['mtime_ns'], entry['mtime_ns']), follow_symlinks=False)
    for entry in reversed(entries):
        if entry['kind'] == 'dir':
            path = out / entry['path']
            os.chmod(path, entry['mode'])
            os.utime(path, ns=(entry['mtime_ns'], entry['mtime_ns']))
    for entry in entries:
        path = out / entry['path']
        info = path.lstat()
        if entry['kind'] == 'file':
            assert info.st_size == entry['bytes'] and digest(path) == entry['sha256'], entry['path']
        elif entry['kind'] == 'symlink':
            assert os.readlink(path) == entry['target'], entry['path']
        elif entry['kind'] == 'fifo':
            assert stat.S_ISFIFO(info.st_mode), entry['path']
        if entry['kind'] != 'symlink':
            assert stat.S_IMODE(info.st_mode) == entry['mode'], entry['path']
    print('Restored and verified {} files, {} symlinks, {} directories, {} FIFOs.'.format(
        sum(e['kind'] == 'file' for e in entries),
        sum(e['kind'] == 'symlink' for e in entries),
        sum(e['kind'] == 'dir' for e in entries),
        sum(e['kind'] == 'fifo' for e in entries)))
