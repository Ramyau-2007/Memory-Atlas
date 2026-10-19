"""Reproducible synthetic comparison; timings/token counts are local estimates, not LLM benchmarks."""
import json, pathlib, sys, time
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from app.db import init_db
from app.memory import add_memory, retrieve, classify, list_memories
from app.agent import token_estimate

def main():
    cases=json.loads((pathlib.Path(__file__).with_name('cases.json')).read_text(encoding='utf-8'))
    db=':memory:'
    # Each independent sqlite in-memory connection is separate; use a temporary file for stable connections.
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        db=os.path.join(tmp,'eval.sqlite3');init_db(db); rows=[]; retained=0; relevant=0; important_total=0; important_retained=0
        for case in cases:
            from app.db import connect
            with connect(db) as conn:
                user_id=conn.execute('INSERT INTO users(username,password_hash) VALUES(?,?)',('user_'+case['id'],'evaluation-only')).lastrowid
            for fact in case['history']:
                c=classify(fact)
                if c.get('retain'): add_memory(user_id,c['content'],c['category'],c['priority'],c['reason'],db_path=db);retained+=1
            start=time.perf_counter(); found=retrieve(user_id,case['query'],db_path=db); prop_ms=(time.perf_counter()-start)*1000
            baseline=' '.join(case['history'])+' '+case['query']; proposed=' '.join(x['content'] for x in found)+' '+case['query']
            target=case['expected_memory']; hit=target is None or any(target.lower() in x['content'].lower() for x in found)
            relevant += int(target is None or hit)
            if target is not None:
                important_total += 1
                important_retained += int(any(target.lower() in x['content'].lower() for x in list_memories(user_id,db_path=db)))
            b=token_estimate(baseline);p=token_estimate(proposed)
            rows.append({'id':case['id'],'baseline_tokens_est':b,'proposed_tokens_est':p,'reduction_pct_est':round((b-p)/b*100,1),'baseline_latency_ms_measured':0.0,'retrieval_latency_ms_measured':round(prop_ms,3),'expected_memory_retained':hit,'retrieved':len(found)})
        print(json.dumps({'notice':'Synthetic local evaluation. Token counts are character estimates; baseline latency is set to 0 because no baseline model call is made. Retrieval latency is wall-clock Python measurement.','cases':rows,'important_memory_retention_accuracy':round(important_retained/max(1,important_total),3),'retrieval_relevance_accuracy':round(relevant/len(cases),3),'storage_efficiency':'SQLite row counts can be measured; meaningful byte comparison is omitted for this tiny dataset.','version_consistency':'Covered by automated lifecycle tests.'},indent=2))
if __name__=='__main__':main()
