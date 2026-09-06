import tempfile
import unittest
from pathlib import Path
from qa_bot.knowledge.computer_cache import ComputerActionCache,identity


class ComputerCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.cache=ComputerActionCache(Path(self.directory.name)/'shared/actions.sqlite3')
        self.nodes=[{'id':'go','value':None,'rect':{'x':1,'y':2,'width':30,'height':40}}]
        self.key,self.canonical=identity('Synthetic task',self.nodes,'a'*64,'https://fixture.example/assets/msOfficeSimulation/run.html?private=token')
        self.action={'action':'click','id':'go','value':None}
    def tearDown(self):
        self.cache.close();self.directory.cleanup()
    def test_observed_is_not_reusable_until_advanced(self):
        self.cache.observe(self.key,self.canonical,'first','1')
        self.assertIsNone(self.cache.lookup(self.key,self.canonical))
        self.cache.promote([(self.key,self.canonical,self.action)],'first')
        self.assertIsNone(self.cache.lookup(self.key,self.canonical))
        action,source,confidence=self.cache.lookup(self.key,self.canonical,require_verified=False)
        self.assertIsNone(confidence)
        self.assertEqual(action['id'],'go');self.assertEqual(source,'first')
        self.assertNotIn('token',self.canonical)
    def test_pixels_task_controls_values_and_rectangles_bound(self):
        self.cache.promote([(self.key,self.canonical,self.action)],'first')
        cases=[('Other task',self.nodes,'a'*64),('Synthetic task',self.nodes,'b'*64),
            ('Synthetic task',[dict(self.nodes[0],value='changed')],'a'*64),
            ('Synthetic task',[dict(self.nodes[0],rect={'x':2,'y':2,'width':30,'height':40})],'a'*64)]
        for task,nodes,digest in cases:
            key,canonical=identity(task,nodes,digest,'https://fixture.example/assets/msOfficeSimulation/run.html')
            self.assertIsNone(self.cache.lookup(key,canonical))
        key,canonical=identity('Synthetic task',self.nodes,'a'*64,'https://other.example/assets/msOfficeSimulation/run.html')
        self.assertIsNone(self.cache.lookup(key,canonical))
    def test_conflict_is_permanently_disabled(self):
        self.cache.promote([(self.key,self.canonical,self.action)],'first')
        self.cache.promote([(self.key,self.canonical,dict(self.action,action='double_click'))],'second')
        self.assertIsNone(self.cache.lookup(self.key,self.canonical))
        self.cache.promote([(self.key,self.canonical,self.action)],'third')
        self.assertIsNone(self.cache.lookup(self.key,self.canonical))
    def test_identity_corruption_rejected(self):
        with self.assertRaises(ValueError):self.cache.lookup(self.key,self.canonical+' ')

    def test_legacy_entries_migrate_unverified(self):
        import sqlite3
        path=Path(self.directory.name)/'legacy.sqlite3'
        db=sqlite3.connect(path)
        db.execute('CREATE TABLE actions(key TEXT PRIMARY KEY,canonical TEXT NOT NULL,action TEXT NOT NULL,source_test TEXT NOT NULL,conflict INTEGER NOT NULL DEFAULT 0)')
        db.execute('INSERT INTO actions VALUES(?,?,?,?,0)',(self.key,self.canonical,'{"action":"click","id":"go"}','old'));db.commit();db.close()
        cache=ComputerActionCache(path)
        try:
            self.assertIsNone(cache.lookup(self.key,self.canonical))
            self.assertIsNotNone(cache.lookup(self.key,self.canonical,require_verified=False))
            self.assertEqual(cache.db.execute('SELECT verification FROM actions').fetchone()[0],'unverified')
        finally:cache.close()
