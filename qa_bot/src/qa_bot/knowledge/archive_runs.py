"""Import explicitly named local QA runs into the shared exact-answer archive."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from qa_bot.knowledge.live_archive import LiveAnswerArchive


def import_run(archive,run):
    counts={'solutions':0,'observations':0}
    for module in ('analytical','listening','sales'):
        path=run/'evidence/owned'/(module+'.sqlite3')
        if not path.is_file():continue
        with sqlite3.connect('file:'+str(path.resolve())+'?mode=ro',uri=True) as source:
            rows=source.execute('SELECT canonical,answer,status FROM answers').fetchall()
        for canonical,payload,status in rows:
            data=json.loads(canonical)
            if module=='analytical':data['media']=[a for a in data['media'] if a[0]!='question-image']
            canonical=json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(',',':'))
            key=hashlib.sha256(canonical.encode()).hexdigest()
            archive.import_solution(key,canonical,payload,run.name+':'+status)
            identity=json.loads(payload)['question_id']
            with archive.db:
                cursor=archive.db.execute('INSERT OR IGNORE INTO observations VALUES(?,?,?,?,?)',
                    (run.name,data['section'],identity,key,json.dumps({'format':'canonical-v1','content':data},ensure_ascii=False)))
                counts['observations']+=cursor.rowcount
            counts['solutions']+=1
    return counts


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--run',type=Path,action='append',required=True)
    args=parser.parse_args();args.destination.parent.mkdir(parents=True,exist_ok=True)
    with LiveAnswerArchive(args.destination,source_test='import') as archive:
        result={str(run):import_run(archive,run) for run in args.run}
        result['totals']={'unique_solutions':archive.db.execute('SELECT count(*) FROM solutions').fetchone()[0],
            'conflicts':archive.db.execute('SELECT count(*) FROM solutions WHERE conflict=1').fetchone()[0],
            'observations':archive.db.execute('SELECT count(*) FROM observations').fetchone()[0]}
    print(json.dumps(result))


if __name__=='__main__':main()
