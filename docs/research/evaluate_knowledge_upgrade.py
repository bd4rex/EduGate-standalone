"""Reproducible local retrieval comparison; never calls an answer-model API.

The authored regression corpus is not a teacher-reviewed classroom study.
Capacity rows duplicate real embeddings and measure search only, not ingestion.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import statistics
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.knowledge import KnowledgeStore, KnowledgeMetadata, KnowledgeScope  # noqa: E402
from app.knowledge_retrieval import conversation_query  # noqa: E402


def old_terms(query):
    query = re.sub(r"\s+", " ", query).strip().lower()
    terms = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2,}", query))
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", query))
    for length in (2, 3, 4):
        terms.update(chinese[index:index+length] for index in range(max(0,len(chinese)-length+1)))
    return sorted(terms,key=len,reverse=True)[:40]


def percentile(values, fraction):
    return round(sorted(values)[min(len(values)-1, int((len(values)-1)*fraction))], 2)


def evaluate(model_dir: Path, sizes: list[int], concurrency: int):
    fixture = json.loads((ROOT / "backend/tests/fixtures/knowledge-evaluation.json").read_text(encoding="utf-8"))
    work = ROOT / "artifacts/knowledge-upgrade" / ("evaluation-" + uuid.uuid4().hex[:10])
    work.mkdir(parents=True)
    store = KnowledgeStore(str(work/"knowledge.sqlite3"),str(work/"files"),model_dir=str(model_dir),semantic_enabled=True)
    if not store.embeddings.load():
        raise RuntimeError("Prepare the real model before running this evaluation")
    for document in fixture["documents"]:
        (store.source_directory("general")/document["name"]).write_text(document["text"],encoding="utf-8")
    scan = store.scan_source("general")
    if scan.errors:
        raise RuntimeError(str(scan.errors))
    files = {file.filename:file for file in store.list_files()}
    for document in fixture["documents"]:
        store.update_file_metadata(files[document["name"]].id, KnowledgeMetadata(**document.get("metadata",{})))
    store.warm_index()
    outputs = {mode:[] for mode in ("legacy","keyword","vector","hybrid")}
    for case in fixture["questions"]:
        question = conversation_query([case["previous"],case["query"]]) if case.get("previous") else case["query"]
        for mode in outputs:
            start = time.perf_counter()
            if mode == "legacy":
                # Legacy had no scope metadata or history; include those limitations.
                terms = old_terms(case["query"])
                ranked = sorted([(sum(document["text"].lower().count(term) for term in terms), document["name"]) for document in fixture["documents"]],reverse=True)
                names = [name for score,name in ranked if score>0][:5]
            else:
                result = store.search_details("general", question, mode=mode, scope=KnowledgeScope(**case.get("scope",{})))
                names = list(dict.fromkeys(hit.filename for hit in result.hits))
            expected = case.get("expected")
            outputs[mode].append({"id":case["id"],"query":case["query"],"expected":expected,"retrieved":names,
                "recall_at_5":expected in names if expected else None,
                "correct_first":bool(names and names[0]==expected) if expected else None,
                "exclusion_passed":case["must_exclude"] not in names if case.get("must_exclude") else None,
                "elapsed_ms":round((time.perf_counter()-start)*1000,2)})
    summary = {}
    for mode, rows in outputs.items():
        positives = [row for row in rows if row["expected"]]
        summary[mode] = {"answerable_cases":len(positives),"recall_at_5_hits":sum(row["recall_at_5"] for row in positives),
            "top_1_hits":sum(row["correct_first"] for row in positives)}
    with store._connect() as conn:
        templates = conn.execute("SELECT text,chapter,page,lexical_terms,embedding,embedding_model FROM chunks WHERE embedding IS NOT NULL").fetchall()
    bench = KnowledgeStore(str(work/"capacity.sqlite3"),str(work/"capacity-files"),model_dir=str(model_dir),semantic_enabled=True)
    bench.embeddings = store.embeddings
    measurements = []
    for size in sizes:
        with bench._connect() as conn:
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM files")
            conn.execute("INSERT INTO files(id,source_id,filename,size_bytes,path,content_hash,created_at,parser_version) VALUES('capacity','general','synthetic.txt',0,'general/synthetic.txt','synthetic','2026-10-24',2)")
            conn.executemany("INSERT INTO chunks(id,file_id,source_id,chunk_index,text,chapter,page,lexical_terms,embedding,embedding_model) VALUES(?,'capacity','general',?,?,?,?,?,?,?)",
                ((f"synthetic-{i}",i,*tuple(templates[i%len(templates)])) for i in range(size)))
            bench._bump_revision(conn)
        started = time.perf_counter()
        bench.warm_index()
        index_ms = (time.perf_counter()-started)*1000
        question = "我家几台电脑怎样共用一个外网号码？"
        times = []
        for _ in range(5):
            started = time.perf_counter()
            assert bench.search("general",question)
            times.append((time.perf_counter()-started)*1000)
        def request(index):
            started = time.perf_counter()
            # Distinct phrasing prevents treating an all-identical embedding-cache burst as general capacity.
            query = fixture["questions"][index%24]["query"] + f"（第{index+1}个提问）"
            bench.search("general", query)
            return (time.perf_counter()-started)*1000
        bench.embeddings._query_cache.clear()
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            burst = list(pool.map(request,range(concurrency)))
        import psutil
        measurements.append({"chunks":size,"derived_index_build_ms":round(index_ms,2),"sequential_median_ms":round(statistics.median(times),2),
            "concurrent_requests":concurrency,"burst_p50_ms":percentile(burst,.5),"burst_p95_ms":percentile(burst,.95),
            "process_rss_mib_after_run":round(psutil.Process().memory_info().rss/1024**2,1)})
    bench.close()
    store.close()
    return {"scope":"Authored development regression cases plus duplicated-chunk local capacity. No answer model, no real student corpus, no OCR load.",
        "python":sys.version.split()[0],"model_signature":store.embeddings.signature,"summary":summary,"cases":outputs,"capacity":measurements}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir",required=True,type=Path)
    parser.add_argument("--sizes",type=int,nargs="+",default=[1000,10000,30000])
    parser.add_argument("--concurrency",type=int,default=40)
    parser.add_argument("--output",type=Path,default=ROOT/"artifacts/knowledge-upgrade/retrieval-evaluation.json")
    args=parser.parse_args()
    if not 1<=args.concurrency<=100 or any(not 1<=size<=100000 for size in args.sizes):
        parser.error("concurrency must be 1..100 and sizes must be 1..100000")
    result=evaluate(args.model_dir.resolve(),args.sizes,args.concurrency)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"summary":result["summary"],"capacity":result["capacity"]},ensure_ascii=False))


if __name__ == "__main__":
    main()
