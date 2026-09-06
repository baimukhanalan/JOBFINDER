"""Exact reuse of prior QA solutions, without claiming an official answer key."""
from dataclasses import asdict, replace
from contextlib import asynccontextmanager
import asyncio
import fcntl
import hashlib
import json
import sqlite3
from pathlib import Path
import time

from qa_bot.execution.validator import validate_answer
from qa_bot.knowledge.bank import decode_answer, fingerprint


def session_archive(session,*,ignored_observation_assets=()):
    return LiveAnswerArchive(getattr(session,'answer_archive_path',session.output/'answer-archive.sqlite3'),
        source_test=getattr(session,'test_id',session.output.parent.name),ignored_observation_assets=ignored_observation_assets)


class LiveAnswerArchive:
    def __init__(self, path, *, source_test, ignored_observation_assets=()):
        self.db=sqlite3.connect(path,timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.source_test=source_test
        self.path=Path(path) if str(path)!=':memory:' else None
        self.ignored_observation_assets=set(ignored_observation_assets)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS solutions(
                key TEXT PRIMARY KEY, canonical TEXT NOT NULL, answer TEXT NOT NULL,
                source TEXT NOT NULL, conflict INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS observations(
                source_test TEXT NOT NULL, section TEXT NOT NULL, question_id TEXT NOT NULL,
                key TEXT NOT NULL, question TEXT NOT NULL,
                PRIMARY KEY(source_test,section,question_id,key));
            CREATE TABLE IF NOT EXISTS usage(
                source_test TEXT NOT NULL, question_id TEXT NOT NULL, key TEXT NOT NULL,
                source TEXT NOT NULL, used_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')

    def __enter__(self):return self
    def __exit__(self,*args):self.db.close()

    @asynccontextmanager
    async def solution_lock(self,q,timeout=60):
        if self.path is None:
            yield
            return
        key,_=self.identity(q)
        directory=self.path.with_suffix('.locks');directory.mkdir(exist_ok=True)
        with (directory/(key+'.lock')).open('a') as stream:
            deadline=time.monotonic()+timeout
            while True:
                try:
                    fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise TimeoutError('shared answer preparation busy')
                    await asyncio.sleep(.05)
            try:yield
            finally:fcntl.flock(stream.fileno(),fcntl.LOCK_UN)

    def identity(self,q):
        # A full-page evidence screenshot includes timer/navigation state. Its
        # exclusion is explicit at the caller; actual question figures remain.
        stable=replace(q,assets=tuple(a for a in q.assets if a.id not in self.ignored_observation_assets))
        return fingerprint(stable)

    def observe(self,q):
        key,_=self.identity(q)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)',
                (self.source_test,q.section,q.question_id,key,json.dumps(asdict(q),ensure_ascii=False)))

    def lookup(self,q):
        key,canonical=self.identity(q)
        row=self.db.execute('SELECT canonical,answer,conflict FROM solutions WHERE key=?',(key,)).fetchone()
        if not row or row[0]!=canonical or row[2]:return None
        answer=replace(decode_answer(row[1]),question_id=q.question_id,content_hash=q.content_hash)
        if validate_answer(q,answer):return None
        with self.db:self.db.execute('INSERT INTO usage(source_test,question_id,key,source) VALUES(?,?,?,?)',
            (self.source_test,q.question_id,key,'previous_exact'))
        return answer

    @staticmethod
    def answer_identity(answer):
        data=asdict(answer)
        return json.dumps({k:data[k] for k in ('kind','selections','text')},sort_keys=True,ensure_ascii=False)

    def save(self,q,answer,source):
        if validate_answer(q,answer):raise ValueError('invalid archived answer')
        key,canonical=self.identity(q)
        self.import_solution(key,canonical,json.dumps(asdict(answer),ensure_ascii=False),source)

    def import_solution(self,key,canonical,payload,source):
        if hashlib.sha256(canonical.encode()).hexdigest()!=key:raise ValueError('archive key mismatch')
        incoming=decode_answer(payload)
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            old=self.db.execute('SELECT canonical,answer FROM solutions WHERE key=?',(key,)).fetchone()
            if old:
                if old[0]!=canonical or self.answer_identity(decode_answer(old[1]))!=self.answer_identity(incoming):
                    self.db.execute('UPDATE solutions SET conflict=1 WHERE key=?',(key,))
            else:
                self.db.execute('INSERT INTO solutions(key,canonical,answer,source) VALUES(?,?,?,?)',(key,canonical,payload,source))
