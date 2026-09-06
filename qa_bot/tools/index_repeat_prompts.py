"""Index explicitly selected captured repeat prompts without regenerating audio."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
from qa_bot.adapters.stt.natively_local import NativelyLocalSTT
from qa_bot.audio.replay_bank import SpeechReplayBank, MacOSAudioConverter


async def run(args):
    root=args.manifest.parent.resolve()
    selected=set(args.question)
    records=[json.loads(line) for line in args.manifest.read_text().splitlines()]
    records=[r for r in records if r['source_profile']==args.profile and
        r['source_test']==args.test and r['source_question'] in selected]
    if {r['source_question'] for r in records}!=selected:
        raise ValueError('all explicitly selected prompts must exist')
    transcripts=root/'transcripts';transcripts.mkdir(exist_ok=True)
    reports=[]
    with SpeechReplayBank(args.database,args.artifacts) as bank:
        for record in records:
            if not re.search(r'/sID/\d+/[^?#]+/question\d+\.mp3$',record['source_path']):
                raise ValueError('not an original repeat sentence')
            source=(root/record['file']).resolve()
            if not source.is_relative_to(root) or hashlib.sha256(source.read_bytes()).hexdigest()!=record['sha256']:
                raise ValueError('original prompt integrity failure')
            wav=transcripts/(record['sha256']+'.wav')
            MacOSAudioConverter().mp3_to_wav(source,wav)
            text_file=transcripts/(record['sha256']+'.txt')
            if text_file.exists():text=text_file.read_text()
            else:
                text=await NativelyLocalSTT(args.project_root).transcribe_wav(wav)
                text_file.write_text(text)
            replay=bank.import_existing(text,text,source,source_profile=args.profile,
                source_test=args.test,source_question=record['source_question'])
            # A text collision may already have a different original recording.
            # Keep that bank entry intact, and report the distinction explicitly.
            reports.append({'question':record['source_question'],'transcript':text,
                'question_key':replay.question_key,'prompt_sha256':record['sha256'],
                'bank_sha256':replay.audio_sha256,'identical_original':record['sha256']==replay.audio_sha256,
                'stt_is_metadata':True,'tts_calls':0})
            print(json.dumps({'indexed':record['source_question'],'identical_original':reports[-1]['identical_original']}),flush=True)
    args.report.write_text(json.dumps(reports,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('manifest','database','artifacts','project-root','report'):
        parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--profile',required=True);parser.add_argument('--test',required=True)
    parser.add_argument('--question',action='append',required=True)
    asyncio.run(run(parser.parse_args()))
