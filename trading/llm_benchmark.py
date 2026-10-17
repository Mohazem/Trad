"""Explicit local benchmark; no model installation and no broker capability."""
import argparse
from dataclasses import asdict
from datetime import datetime
import json
import subprocess
import threading
import time
from .ai_contracts import sample_packet
from .ai_pipeline import AdvisoryPipeline
from .connected import ConnectedError, OllamaProvider, local_json
from .model_routing import configured_roles
from .models import canonical


def ollama_rss_bytes():
    """Sample process RSS, not VRAM or guaranteed model working-set size.

    ps comm contains executable names, never environment or request arguments.
    A missing measurement is None, not zero memory usage.
    """
    try:
        result=subprocess.run(['ps','-axo','rss=,comm='],capture_output=True,text=True,check=True,timeout=2)
        total=0;found=False
        for line in result.stdout.splitlines():
            parts=line.strip().split(None,1)
            if len(parts)==2 and 'ollama' in parts[1].lower():
                total+=int(parts[0])*1024;found=True
        return total if found else None
    except (OSError,ValueError,subprocess.SubprocessError):return None


def benchmark(pipeline, repeats=3):
    if type(repeats) is not int or not 1<=repeats<=20:raise ValueError('Invalid repetitions')
    stop=threading.Event();samples=[]
    def sample():
        while not stop.is_set():
            value=ollama_rss_bytes()
            if value is not None:samples.append(value)
            stop.wait(.2)
    sampler=threading.Thread(target=sample,daemon=True);sampler.start()
    results=[];started=time.monotonic()
    try:
        packet=sample_packet();now=datetime.fromisoformat(packet['asof'])
        for _ in range(repeats):
            result=pipeline.evaluate(packet,now);results.append(asdict(result))
            if result.code=='provider_timeout':break
    finally:
        stop.set();sampler.join(3)
    attempts=sum(r['calls'] for r in results)
    valid=sum(m['json_valid'] for r in results for m in r['role_metrics'].values())
    return {'input_kind':'synthetic_fixture','new_inference':True,'runs':results,
        'wall_seconds':time.monotonic()-started,'json_valid_rate':valid/attempts if attempts else None,
        'abstention_rate':sum(r['action']=='abstain' for r in results)/len(results),
        'sampled_peak_ollama_rss_bytes':max(samples) if samples else None,
        'memory_method':'Sum of Ollama process RSS sampled every ~200ms; can miss spikes and double-count shared pages.',
        'warning':'Software/latency comparison only; no trading quality or profitability claim.'}


def main():
    p=argparse.ArgumentParser(description='Explicit mono/multi Ollama benchmark; never downloads weights')
    p.add_argument('--single-model',default='qwen2.5:1.5b')
    p.add_argument('--analyst-model',default='qwen3:8b');p.add_argument('--critic-model',default='qwen3:14b')
    p.add_argument('--synthesizer-model',default='qwen3:8b');p.add_argument('--repeats',type=int,default=3)
    p.add_argument('--output',required=True);args=p.parse_args()
    try:
        installed=local_json('/api/tags',timeout=2).get('models',[])
        names={m.get('name') for m in installed}
        report={'mono':{'status':'not_run_model_missing'},'multi':{'status':'not_run_models_missing'}}
        if args.single_model in names:
            report['mono']=benchmark(AdvisoryPipeline(OllamaProvider(args.single_model,unload_after=True),timeout_seconds=30),args.repeats)
        settings={'TRAD_LLM_ROUTING_ENABLED':'1',**{'TRAD_'+role.upper()+'_MODEL':getattr(args,role+'_model') for role in ('analyst','critic','synthesizer')}}
        try:roles=configured_roles(settings,installed,lambda m:OllamaProvider(m,unload_after=True))
        except ValueError:roles=None
        if roles is not None:
            report['multi']=benchmark(AdvisoryPipeline(role_providers=roles,timeout_seconds=30),args.repeats)
        with open(args.output,'x',encoding='utf-8') as stream:stream.write(canonical(report)+'\n')
        print('Local benchmark report written; absent configurations were not run.')
    except (ConnectedError,OSError,ValueError):p.exit(1,'Benchmark unavailable: check Ollama, installed models and output path. No download performed.\n')


if __name__=='__main__':main()
