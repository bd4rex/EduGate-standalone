"""Chinese lexical retrieval and local semantic search using LanceDB/FastEmbed.

SQLite remains the authoritative store. LanceDB is a rebuildable index; its
manifest identifies the exact SQLite revision and embedding model it contains.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

import jieba

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_REPOSITORY = "Qdrant/bge-small-zh-v1.5"
MODEL_REVISION = "46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59"
MODEL_FILES = ("model_optimized.onnx", "config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json")
DIMENSIONS = 512
INDEX_VERSION = 1
STOP_WORDS = set("的 了 是 在 和 与 及 或 我 我们 你 你们 他 她 它 他们 这 那 一个 一些 什么 怎么 怎样 如何 为什么 请 请问 帮 帮我 能 可以 吗 呢 吧 呀 啊 老师 您 您好 你好 谢谢 解释 说明 这个 那个 进行 使用 相关 关于 根据 对 于 为 有 都 要 就 也 还 再 给 说 一下 学生 内容 理解 希望".split())
jieba.setLogLevel(logging.WARNING)
_TOKENIZER_LOCK = threading.Lock()


@lru_cache(maxsize=4096)
def terms(text: str) -> tuple[str, ...]:
    with _TOKENIZER_LOCK:
        tokens = list(jieba.cut_for_search(text.lower()))
    # Keep exact identifiers alongside segmented text, including IPv4/CIDR.
    identifiers = re.findall(r"[a-z0-9_]+(?:[./:-][a-z0-9_]+)*", text.lower())
    words = [word.strip() for word in tokens if re.search(r"[\w\u4e00-\u9fff]", word)]
    return tuple(dict.fromkeys(word for word in identifiers + words if word and word not in STOP_WORDS))


def lexical_text(text: str) -> str:
    # Preserve occurrence counts for BM25; query terms themselves are deduplicated.
    with _TOKENIZER_LOCK:
        words = jieba.cut_for_search(text.lower())
        return " ".join(word.strip() for word in words if word.strip() not in STOP_WORDS and re.search(r"[\w\u4e00-\u9fff]", word))


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def conversation_query(questions: list[str]) -> str:
    if not questions:
        return ""
    latest = questions[-1].strip()
    if len(questions) > 1 and len(latest) <= 100 and re.search(r"^(那|它|这个|这种|为什么|再|能再|你能再|请再|举|继续|还有|what about|why|how about)|例子|刚才|上面", latest, re.IGNORECASE):
        return questions[-2][-500:] + "\n追问：" + latest
    return latest


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class LocalEmbeddings:
    """No network access on requests. Download is a separate teacher action."""

    def __init__(self, directory: Path, *, enabled: bool = True) -> None:
        self.directory = directory.resolve()
        self.enabled = enabled
        self._model: Any = None
        self._signature = ""
        self._lock = threading.RLock()
        self._query_cache: dict[str, list[float]] = {}
        self.error = ""

    @property
    def signature(self) -> str:
        return self._signature

    def _load_manifest(self) -> Path | None:
        manifest = self.directory / "model.json"
        if not self.enabled or not manifest.exists():
            return None
        data = json.loads(manifest.read_text(encoding="utf-8"))
        directory = (self.directory / data["relative_path"]).resolve()
        if not directory.is_relative_to(self.directory) or data["model"] != MODEL_NAME:
            raise ValueError("Invalid embedding model manifest")
        model_file = directory / "model_optimized.onnx"
        if not model_file.is_file():
            return None
        digest = _digest(model_file)
        if digest != data["sha256"]:
            raise ValueError("Embedding model checksum does not match")
        assets = data.get("files")
        if assets:
            if set(assets) != set(MODEL_FILES):
                raise ValueError("Embedding model asset manifest is incomplete")
            for filename, expected in assets.items():
                target = (directory / filename).resolve()
                if not target.is_relative_to(self.directory) or _digest(target) != expected:
                    raise ValueError("Embedding model asset checksum does not match")
            digest = hashlib.sha256(json.dumps(assets, sort_keys=True).encode()).hexdigest()
        self._signature = MODEL_NAME + ":" + digest
        return directory

    def load(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._model is not None:
                return True
            try:
                directory = self._load_manifest()
                if directory is None:
                    return False
                from fastembed import TextEmbedding
                self._model = TextEmbedding(MODEL_NAME, specific_model_path=str(directory), local_files_only=True, threads=2)
                self.error = ""
                return True
            except Exception:
                self.error = "本地语义模型不可用，请重新准备模型；当前仅使用关键词检索。"
                self._signature = ""
                logging.getLogger(__name__).exception("Local embedding model could not be loaded")
                return False

    def prepare(self) -> None:
        if not self.enabled:
            raise RuntimeError("语义检索已在运行配置中禁用。")
        with self._lock:
            from fastembed import TextEmbedding
            from huggingface_hub import snapshot_download
            self.directory.mkdir(parents=True, exist_ok=True)
            model_path = Path(snapshot_download(MODEL_REPOSITORY, revision=MODEL_REVISION,
                cache_dir=str(self.directory), allow_patterns=list(MODEL_FILES), token=False)).resolve()
            if not model_path.is_relative_to(self.directory):
                raise ValueError("Model download escaped its cache directory")
            assets = {filename: _digest(model_path / filename) for filename in MODEL_FILES}
            digest = hashlib.sha256(json.dumps(assets, sort_keys=True).encode()).hexdigest()
            model = TextEmbedding(MODEL_NAME, specific_model_path=str(model_path), local_files_only=True, threads=2)
            manifest = {
                "model": MODEL_NAME, "relative_path": model_path.relative_to(self.directory).as_posix(),
                "sha256": assets["model_optimized.onnx"], "files": assets,
                "revision": MODEL_REVISION, "dimensions": DIMENSIONS, "license": "MIT",
                "source": "https://huggingface.co/Qdrant/bge-small-zh-v1.5",
            }
            temporary = self.directory / "model.json.tmp"
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.directory / "model.json")
            self._model = model
            self._signature = MODEL_NAME + ":" + digest
            self._query_cache.clear()
            self.error = ""

    def passages(self, texts: list[str]) -> list[list[float]]:
        if not self.load():
            return []
        result = []
        for start in range(0, len(texts), 16):
            with self._lock:
                result.extend(vector.tolist() for vector in self._model.passage_embed(texts[start:start+16], batch_size=16))
            time.sleep(0)  # Give classroom queries a turn between ingestion batches.
        return result

    def query(self, text: str) -> list[float] | None:
        with self._lock:
            if not self.load():
                return None
            # Preserve the question at the end of a long prompt as well as its context.
            text = text if len(text) <= 450 else text[:150] + "\n" + text[-300:]
            if text not in self._query_cache:
                if len(self._query_cache) >= 256:
                    self._query_cache.clear()
                self._query_cache[text] = next(self._model.query_embed(text)).tolist()
            return self._query_cache[text]


class RetrievalIndex:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self._db: Any = None
        self._table: Any = None
        self._identity = ""
        self._lock = threading.RLock()

    def ensure(self, identity: str, rows_factory) -> None:
        with self._lock:
            if self._table is not None and self._identity == identity:
                return
            import lancedb
            import pyarrow as pa
            self.directory.mkdir(parents=True, exist_ok=True)
            if self._db is None:
                self._db = lancedb.connect(str(self.directory))
            manifest = self.directory / "edugate-index.json"
            if manifest.exists():
                try:
                    saved = json.loads(manifest.read_text(encoding="utf-8"))
                    if saved.get("identity") == identity:
                        self._table = self._db.open_table("chunks")
                        self._identity = identity
                        return
                except (ValueError, OSError):
                    pass
            schema = pa.schema([
                ("chunk_id", pa.string()), ("source_id", pa.string()), ("file_id", pa.string()),
                ("filename", pa.string()), ("content_hash", pa.string()), ("chapter", pa.string()),
                ("page", pa.int32()), ("chunk_index", pa.int32()), ("text", pa.string()),
                ("terms", pa.string()), ("subject", pa.string()), ("grade", pa.string()),
                ("edition", pa.string()), ("term", pa.string()), ("audience", pa.string()),
                ("has_vector", pa.bool_()), ("vector", pa.list_(pa.float32(), DIMENSIONS)),
            ])
            rows = rows_factory()
            self._table = self._db.create_table("chunks", data=rows or None, schema=schema, mode="overwrite")
            if rows:
                from lancedb.index import FTS, BTree
                self._table.create_index("terms", config=FTS(base_tokenizer="whitespace", stem=False, remove_stop_words=False, ascii_folding=False))
                self._table.create_index("source_id", config=BTree())
                self._table.create_index("file_id", config=BTree())
            temporary = manifest.with_suffix(".tmp")
            temporary.write_text(json.dumps({"identity": identity, "version": INDEX_VERSION}), encoding="utf-8")
            temporary.replace(manifest)
            self._identity = identity

    def snapshot(self):
        with self._lock:
            return self._table

    def search(self, query: str, where: str, vector: list[float] | None, *, limit: int = 5, mode: str = "hybrid", table: Any = None) -> list[dict[str, Any]]:
        table = table if table is not None else self.snapshot()
        import pyarrow as pa
        from lancedb.rerankers import RRFReranker
        if table is None or table.count_rows() == 0:
            return []
        candidate_limit = max(20, min(80, limit * 4))
        query_terms = " ".join(terms(query)[:160])
        keyword = pa.table({})
        semantic = pa.table({})
        if query_terms and mode != "vector":
            keyword = table.search(query_terms, query_type="fts", fts_columns="terms").where(where, prefilter=True).limit(candidate_limit).with_row_id(True).to_arrow()
        if vector is not None and mode != "keyword":
            semantic = table.search(vector, vector_column_name="vector").distance_type("cosine").where(f"({where}) AND has_vector = true", prefilter=True).limit(candidate_limit).with_row_id(True).to_arrow()
            # Similarity is a candidate filter, never a claim of evidence sufficiency.
            if len(semantic):
                import pyarrow.compute as pc
                semantic = semantic.filter(pc.less_equal(semantic["_distance"], 0.58))
        if not len(keyword) and not len(semantic):
            return []
        similarity = {row["chunk_id"]: 1.0 - row["_distance"] for row in semantic.to_pylist()}
        lexical = {row["chunk_id"]: row["_score"] for row in keyword.to_pylist()}
        if len(keyword) and len(semantic):
            combined = RRFReranker().rerank_hybrid(query, semantic, keyword).to_pylist()
        else:
            combined = (keyword if len(keyword) else semantic).to_pylist()
            for rank, row in enumerate(combined, 1):
                row["_relevance_score"] = 1 / (60 + rank)
        output = []
        for row in combined:
            row["semantic_score"] = similarity.get(row["chunk_id"])
            row["keyword_score"] = lexical.get(row["chunk_id"])
            row.pop("vector", None)
            row.pop("terms", None)
            output.append(row)
            if len(output) >= limit:
                break
        return output
