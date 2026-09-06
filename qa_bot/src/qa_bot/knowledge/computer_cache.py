"""Exact observed simulation actions; advancement alone never verifies success."""
import hashlib
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

ACTION_FIELDS=('action','id','value','target_id','x','y','target_x','target_y')


def identity(task,nodes,screenshot_sha256,frame_url):
    parsed=urlsplit(frame_url)
    if len(screenshot_sha256)!=64 or any(c not in '0123456789abcdef' for c in screenshot_sha256):
        raise ValueError('invalid screenshot hash')
    # Query strings may carry invitation tokens. The pixels and controls bind
    # the rendered variation; its hosting origin/path additionally bound reuse.
    canonical=json.dumps({'version':1,'task':task,'nodes':nodes,'screenshot_sha256':screenshot_sha256,
        'frame_origin':f'{parsed.scheme}://{parsed.netloc}','frame_path':parsed.path},sort_keys=True,separators=(',',':'),ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest(),canonical


class ComputerActionCache:
    def __init__(self,path):
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(path,timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS actions(key TEXT PRIMARY KEY, canonical TEXT NOT NULL,
            action TEXT NOT NULL, source_test TEXT NOT NULL, conflict INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS observations(source_test TEXT NOT NULL, question TEXT NOT NULL,
            key TEXT NOT NULL, canonical TEXT NOT NULL, observed_at TEXT DEFAULT CURRENT_TIMESTAMP);
          CREATE TABLE IF NOT EXISTS usage(source_test TEXT NOT NULL, question TEXT NOT NULL,
            key TEXT NOT NULL, original_test TEXT NOT NULL, used_at TEXT DEFAULT CURRENT_TIMESTAMP);
        ''')
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            columns={r[1] for r in self.db.execute('PRAGMA table_info(actions)')}
            if 'verification' not in columns:self.db.execute("ALTER TABLE actions ADD COLUMN verification TEXT NOT NULL DEFAULT 'unverified'")
            if 'observed_confidence' not in columns:self.db.execute('ALTER TABLE actions ADD COLUMN observed_confidence REAL')
            self.db.execute('CREATE TABLE IF NOT EXISTS question_outcomes(source_test TEXT NOT NULL, question TEXT, advanced INTEGER NOT NULL, verification TEXT NOT NULL, events TEXT NOT NULL)')

    def close(self):self.db.close()

    def observe(self,key,canonical,source_test,question):
        self._validate(key,canonical)
        with self.db:self.db.execute('INSERT INTO observations(source_test,question,key,canonical) VALUES(?,?,?,?)',
            (source_test,str(question),key,canonical))

    @staticmethod
    def _validate(key,canonical):
        if hashlib.sha256(canonical.encode()).hexdigest()!=key:raise ValueError('cache identity mismatch')

    def lookup(self,key,canonical,*,require_verified=True):
        self._validate(key,canonical)
        row=self.db.execute('SELECT canonical,action,source_test,conflict,verification,observed_confidence FROM actions WHERE key=?',(key,)).fetchone()
        if not row or row[0]!=canonical or row[3]:return None
        if require_verified and row[4]!='verified_success':return None
        return json.loads(row[1]),row[2],row[5]

    def used(self,key,source_test,question,original_test):
        with self.db:self.db.execute('INSERT INTO usage(source_test,question,key,original_test) VALUES(?,?,?,?)',
            (source_test,str(question),key,original_test))

    def record_outcome(self,source_test,question,events):
        # No vendor event contract has been verified. Retain the actual safe
        # values for auditing; neither ready nor generic message is success.
        with self.db:self.db.execute('INSERT INTO question_outcomes VALUES(?,?,?,?,?)',
            (source_test,str(question),1,'unverified',json.dumps(events,sort_keys=True)))

    def promote(self,steps,source_test,*,question=None,events=(),advanced_at_ms=None):
        """Archive a sequence; only the validated typed task result verifies it."""
        from qa_bot.knowledge.computer_protocol import outcome
        verification=outcome(question,steps,events,advanced_at_ms)
        with self.db:
            # Serialize read-then-insert across concurrent browser workers.
            self.db.execute('BEGIN IMMEDIATE')
            for key,canonical,action in steps:
                self._validate(key,canonical)
                payload=json.dumps({k:action.get(k) for k in ACTION_FIELDS},sort_keys=True,separators=(',',':'))
                old=self.db.execute('SELECT canonical,action,verification FROM actions WHERE key=?',(key,)).fetchone()
                if old:
                    if old[:2]!=(canonical,payload):self.db.execute('UPDATE actions SET conflict=1 WHERE key=?',(key,))
                    elif verification['verification']=='verified_success':
                        self.db.execute('UPDATE actions SET verification=? WHERE key=?',('verified_success',key))
                    elif verification['verification']=='reported_failure':
                        if old[2]=='verified_success':self.db.execute('UPDATE actions SET conflict=1,verification=? WHERE key=?',('conflicting_outcomes',key))
                        else:self.db.execute('UPDATE actions SET verification=? WHERE key=?',('reported_failure',key))
                else:self.db.execute('INSERT INTO actions(key,canonical,action,source_test,observed_confidence,verification) VALUES(?,?,?,?,?,?)',
                    (key,canonical,payload,source_test,action.get('confidence'),verification['verification']))

            self.db.execute('INSERT INTO question_outcomes VALUES(?,?,?,?,?)',
                (source_test,str(question),1,verification['verification'],json.dumps({'outcome':verification,'events':list(events)},sort_keys=True)))
        return verification
