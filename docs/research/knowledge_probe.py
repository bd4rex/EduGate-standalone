"""Reproduce the knowledge retrieval review without reading live classroom data.

Run from any directory with the project's backend dependencies installed:
    python docs/research/knowledge_probe.py --output output/knowledge-probe.json

Uses the public repository sample and synthetic repeated chunks only. It does
not import app.state, contact a model provider, or change application settings.
Results describe retrieval behavior, not answer accuracy or concurrency capacity.
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.knowledge import KnowledgeStore, chunk_text, extract_text, tokenize  # noqa: E402


def source_identity() -> dict:
    paths = [
        "backend/app/knowledge.py",
        "backend/app/chat_service.py",
        "backend/app/routers/chat.py",
        "samples/ip_teaching_knowledge_base.txt",
    ]
    result = {"files": []}
    try:
        result["head"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        result["head"] = None
    for name in paths:
        data = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
        entry = {"path": name, "sha256_lf": hashlib.sha256(data).hexdigest()}
        if result["head"]:
            committed = subprocess.check_output(
                ["git", "show", f"HEAD:{name}"], cwd=ROOT
            ).replace(b"\r\n", b"\n")
            entry["matches_head"] = data == committed
        result["files"].append(entry)
    return result


def run(sizes: list[int], repeats: int) -> dict:
    result = {
        "captured_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "sqlite": sqlite3.sqlite_version,
        },
        "source": source_identity(),
        "scope": {
            "corpus": "repository IP teaching sample and synthetic repeated chunks",
            "model_api_called": False,
            "live_application_data_read": False,
            "concurrency_tested": False,
            "answer_accuracy_measured": False,
            "chunk_index_base": 0,
        },
    }
    temp_parent = Path(tempfile.gettempdir()).resolve()
    work = Path(tempfile.mkdtemp(prefix="edugate-kb-probe-", dir=temp_parent)).resolve()
    try:
        store = KnowledgeStore(str(work / "sample.sqlite3"), str(work / "files"))
        sample = store.source_directory("general") / "ip_teaching_knowledge_base.txt"
        shutil.copyfile(ROOT / "samples" / sample.name, sample)
        store.scan_source("general")
        text = extract_text(sample)
        result["sample"] = {
            "file_count": len(store.list_files()),
            "chunk_count": store.get_source("general").chunk_count,
            "normalized_char_count": len(text),
        }
        cases = [
            ("exact_term", "什么是IP地址？"),
            ("exact_term", "IPv6是多少位？"),
            ("exact_term", "NAT有什么作用？"),
            ("semantic_related_not_a_gold_answer", "我家几台电脑怎样共用一个外网号码？"),
            ("followup_requires_conversation", "你能再举个例子吗？"),
            ("sample_has_no_answer_evidence", "NAT是谁发明的？"),
            ("off_topic_with_shared_word", "网络游戏充值怎么操作？"),
            ("exact_term", "子网掩码怎么写成斜杠？"),
        ]
        result["retrieval_probes"] = []
        for kind, question in cases:
            terms = tokenize(question)
            hits = store.search("general", question)
            result["retrieval_probes"].append({
                "kind": kind,
                "question": question,
                "hit_count": len(hits),
                "top_two": [{
                    "chunk_index": hit.chunk_index,
                    "score": hit.score,
                    "matching_terms": [term for term in terms if term in hit.text.lower()],
                } for hit in hits[:2]],
            })
        long_question = (
            "老师您好我是一名刚刚开始学习这门课程的学生最近在复习课堂上讲过的相关内容"
            "但是一直没有完全理解所以希望您可以按照教材中的说法用比较容易理解的语言"
            "给我详细解释NAT"
        )
        result["long_query_probe"] = {
            "question": long_question,
            "retained_term_count": len(tokenize(long_question)),
            "nat_retained": "nat" in tokenize(long_question),
            "hit_count": len(store.search("general", long_question)),
        }
        store.upsert_source("filename-test", "标题实验")
        (store.source_directory("filename-test") / "量化.md").write_text(
            "把连续变化的取值映射到有限的离散等级。", encoding="utf-8"
        )
        store.scan_source("filename-test")
        result["filename_probe"] = {
            "filename": "量化.md",
            "question": "量化",
            "hit_count": len(store.search("filename-test", "量化")),
        }
        bench = KnowledgeStore(str(work / "bench.sqlite3"), str(work / "bench-files"))
        base = chunk_text(text)
        query = "为什么私有IP地址需要经过NAT才能访问公网"
        result["benchmark"] = {
            "query": query,
            "query_term_count": len(tokenize(query)),
            "repeats": repeats,
            "method": "Duplicate normalized sample chunks; measure sequential search() calls including source lookup; no model, no concurrent requests.",
            "rows": [],
        }
        for size in sizes:
            with bench._connect() as conn:
                conn.execute("DELETE FROM chunks")
                conn.execute("DELETE FROM files")
                conn.execute("INSERT INTO files VALUES(?,?,?,?,?,?,?,?)", (
                    "benchfile", "general", "synthetic.txt", "text/plain", size * 900,
                    "general/synthetic.txt", "synthetic", "2026-10-22T00:00:00+00:00",
                ))
                conn.executemany("INSERT INTO chunks VALUES(?,?,?,?,?)", (
                    (f"c{i}", "benchfile", "general", i,
                     (base[i % len(base)] + " ")[:880] + f" 合成资料编号{i}")
                    for i in range(size)
                ))
            elapsed = []
            for _ in range(repeats):
                started = time.perf_counter()
                hits = bench.search("general", query)
                elapsed.append(round((time.perf_counter() - started) * 1000, 2))
            result["benchmark"]["rows"].append({
                "chunks": size,
                "returned_hits": len(hits),
                "elapsed_ms": elapsed,
                "median_ms": round(statistics.median(elapsed), 2),
            })
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE ft USING fts5(text)")
            conn.execute("INSERT INTO ft VALUES(?)", ("子网掩码用于区分网络部分和主机部分",))
            result["fts5_default_chinese_probe"] = {
                "query": "子网掩码",
                "hit_count": conn.execute(
                    "SELECT COUNT(*) FROM ft WHERE ft MATCH ?", ("子网掩码",)
                ).fetchone()[0],
                "interpretation": "Default unicode61 is not a Chinese word segmenter; configure/preprocess Chinese explicitly.",
            }
        except sqlite3.OperationalError as error:
            result["fts5_default_chinese_probe"] = {"unavailable": str(error)}
        finally:
            conn.close()
        return result
    finally:
        # SQLite transaction contexts do not close their connections; collect
        # the short-lived store connections before deleting this owned directory.
        gc.collect()
        if work.parent != temp_parent or not work.name.startswith("edugate-kb-probe-"):
            raise RuntimeError("Refusing cleanup outside the owned probe directory")
        shutil.rmtree(work)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 10000, 30000])
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 1 or any(size < 1 or size > 100000 for size in args.sizes):
        parser.error("repeats must be positive; each size must be between 1 and 100000")
    payload = json.dumps(run(args.sizes, args.repeats), ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
