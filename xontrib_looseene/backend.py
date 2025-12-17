import os
import sys
import re
import json
import zlib
import mmap
import math
import struct
import shutil
import threading
import heapq
import time
import uuid
import hashlib
import collections
from collections import defaultdict, Counter
from typing import Dict, List, Tuple, Type, Optional, Set

try:
    from xonsh.history.base import History
except ImportError:

    class History:
        def __init__(self, **kwargs):
            pass


_REGISTRY: Dict[str, 'IndexEngine'] = {}
_REGISTRY_LOCK = threading.Lock()
POSTING_STRUCT = struct.Struct('<QI')


class TextProcessor:
    SUFFIXES = re.compile(r'(ing|ed|es|ly|er|or|tion|ment|est|al|s)$')
    TOKEN_RE = re.compile(r'\w+')

    @staticmethod
    def stem(word: str) -> str:
        if len(word) < 4:
            return word
        if word.endswith('ing'):
            if len(word) < 5:
                return word
        return TextProcessor.SUFFIXES.sub('', word)

    @staticmethod
    def process(text: str) -> List[str]:
        if not text:
            return []
        raw_words = TextProcessor.TOKEN_RE.findall(text.lower())
        return [TextProcessor.stem(w) for w in raw_words]


class BM25:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b

    def score(self, tf: int, doc_len: int, avg_dl: float, idf: float) -> float:
        numerator = tf * (self.k1 + 1)
        denominator = tf + self.k1 * (1 - self.b + self.b * (doc_len / avg_dl))
        return idf * (numerator / denominator)


class DiskSegment:
    def __init__(self, dir_path: str):
        self.dir_path = dir_path
        self.vocab: Dict[str, Tuple[int, int]] = {}
        self.doc_index: Dict[int, Tuple[int, int, int]] = {}
        self.files = {}
        self.mm_postings = None
        self.mm_docs = None
        try:
            p_path = os.path.join(dir_path, 'postings.bin')
            d_path = os.path.join(dir_path, 'docs.bin')
            if not os.path.exists(p_path):
                open(p_path, 'wb').close()
            if not os.path.exists(d_path):
                open(d_path, 'wb').close()
            self.files['postings'] = open(p_path, 'rb')
            self.files['docs'] = open(d_path, 'rb')
            if os.path.getsize(p_path) > 0:
                self.mm_postings = mmap.mmap(self.files['postings'].fileno(), 0, access=mmap.ACCESS_READ)
            if os.path.getsize(d_path) > 0:
                self.mm_docs = mmap.mmap(self.files['docs'].fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            self.close()
            raise
        self._load_vocab()
        self._load_doc_index()

    def _load_vocab(self):
        vp = os.path.join(self.dir_path, 'vocab.json')
        if os.path.exists(vp):
            with open(vp, 'r', encoding='utf-8') as f:
                self.vocab = json.load(f)

    def _load_doc_index(self):
        dp = os.path.join(self.dir_path, 'doc_idx.json')
        if os.path.exists(dp):
            with open(dp, 'r') as f:
                raw = json.load(f)
                self.doc_index = {int(k): tuple(v) for k, v in raw.items()}

    def get_postings(self, term: str) -> List[Tuple[int, int]]:
        if self.mm_postings is None or term not in self.vocab:
            return []
        offset, length = self.vocab[term]
        try:
            raw_bytes = zlib.decompress(self.mm_postings[offset : offset + length])
            results = []
            last_doc_id = 0
            for delta_id, tf in POSTING_STRUCT.iter_unpack(raw_bytes):
                doc_id = last_doc_id + delta_id
                results.append((doc_id, tf))
                last_doc_id = doc_id
            return results
        except Exception:
            return []

    def get_document(self, doc_id: int) -> Optional[Dict]:
        if self.mm_docs is None or doc_id not in self.doc_index:
            return None
        offset, length, _ = self.doc_index[doc_id]
        try:
            return json.loads(zlib.decompress(self.mm_docs[offset : offset + length]).decode('utf-8'))
        except Exception:
            return None

    def get_doc_len(self, doc_id: int) -> int:
        return self.doc_index[doc_id][2] if doc_id in self.doc_index else 0

    def close(self):
        if self.mm_postings:
            self.mm_postings.close()
        if self.mm_docs:
            self.mm_docs.close()
        for f in self.files.values():
            f.close()


class SegmentWriter:
    @staticmethod
    def write(base_dir: str, seg_id: str, inverted_index: Dict, docs: Dict, doc_lens: Dict):
        seg_dir = os.path.join(base_dir, f'seg_{seg_id}')
        os.makedirs(seg_dir, exist_ok=True)
        vocab, doc_index = {}, {}
        with open(os.path.join(seg_dir, 'postings.bin'), 'wb') as f_post:
            curr = 0
            for term in sorted(inverted_index.keys()):
                postings = sorted(inverted_index[term], key=lambda x: x[0])
                buf = bytearray()
                last = 0
                for doc_id, tf in postings:
                    buf.extend(POSTING_STRUCT.pack(doc_id - last, tf))
                    last = doc_id
                comp = zlib.compress(buf)
                f_post.write(comp)
                vocab[term] = (curr, len(comp))
                curr += len(comp)
        with open(os.path.join(seg_dir, 'docs.bin'), 'wb') as f_docs:
            curr = 0
            for doc_id, data in docs.items():
                comp = zlib.compress(json.dumps(data).encode('utf-8'))
                f_docs.write(comp)
                doc_index[doc_id] = (curr, len(comp), doc_lens.get(doc_id, 0))
                curr += len(comp)
        with open(os.path.join(seg_dir, 'vocab.json'), 'w', encoding='utf-8') as f:
            json.dump(vocab, f)
        with open(os.path.join(seg_dir, 'doc_idx.json'), 'w') as f:
            json.dump(doc_index, f)
        return seg_dir


class IndexEngine:
    def __init__(self, name: str, path: str):
        self.path = path
        self.mem_docs = {}
        self.mem_doc_lens = {}
        self.mem_inverted = defaultdict(lambda: defaultdict(int))
        self.stats = {'total_docs': 0, 'total_len': 0, 'doc_freqs': Counter()}
        self.seen_metadata: Dict[str, Dict] = {}
        self.last_added_hash = None
        self.last_added_id = None
        self.segments = []
        self._lock = threading.RLock()
        if self.path:
            if not os.path.exists(self.path):
                os.makedirs(self.path, exist_ok=True)
            self._load_stats()
            self._load_segments()

    def _load_stats(self):
        try:
            with open(os.path.join(self.path, 'stats.json'), 'r') as f:
                d = json.load(f)
                self.stats.update(d)
                self.stats['doc_freqs'] = Counter(d['doc_freqs'])
                raw_hashes = d.get('seen_hashes', [])
                if isinstance(raw_hashes, list):
                    self.seen_metadata = {h: {'cnt': 1, 'cmt': ''} for h in raw_hashes}
                else:
                    self.seen_metadata = raw_hashes
        except:
            pass

    def _save_stats(self):
        with open(os.path.join(self.path, 'stats.json'), 'w') as f:
            json.dump({**self.stats, 'seen_hashes': self.seen_metadata}, f)

    def _load_segments(self):
        for name in sorted(os.listdir(self.path)):
            if name.startswith('seg_'):
                try:
                    self.segments.append(DiskSegment(os.path.join(self.path, name)))
                except:
                    pass

    def _get_cmd_hash(self, text: str) -> str:
        return hashlib.md5(text.strip().encode('utf-8')).hexdigest()

    def add(self, doc: Dict):
        cmd = doc.get('inp', '').strip()
        if not cmd:
            return
        cmd_hash = self._get_cmd_hash(cmd)
        with self._lock:
            meta = self.seen_metadata.get(cmd_hash, {'cnt': 0, 'cmt': ''})
            if 'cnt' in doc:
                current_count = doc['cnt']
            else:
                current_count = meta['cnt'] + 1
            new_comment = doc.get('cmt', meta['cmt'])
            self.seen_metadata[cmd_hash] = {'cnt': current_count, 'cmt': new_comment}
            if self.last_added_hash == cmd_hash and self.last_added_id in self.mem_docs:
                self.mem_docs[self.last_added_id]['cnt'] = current_count
                self.mem_docs[self.last_added_id]['cmt'] = new_comment
                return
            doc['cnt'] = current_count
            doc['cmt'] = new_comment
            doc_id = doc['id']
            self.mem_docs[doc_id] = doc
            self.last_added_hash = cmd_hash
            self.last_added_id = doc_id
            tokens = TextProcessor.process(cmd)
            self.mem_doc_lens[doc_id] = len(tokens)
            for t in tokens:
                self.mem_inverted[t][doc_id] += 1
                self.stats['doc_freqs'][t] += 1
            self.stats['total_docs'] += 1
            self.stats['total_len'] += len(tokens)

    def flush(self):
        with self._lock:
            if not self.mem_docs:
                return
            inv = {t: list(d.items()) for t, d in self.mem_inverted.items()}
            path = SegmentWriter.write(self.path, str(time.time_ns()), inv, self.mem_docs, self.mem_doc_lens)
            self.segments.append(DiskSegment(path))
            self.mem_docs.clear()
            self.mem_doc_lens.clear()
            self.mem_inverted.clear()
            self.last_added_hash = None
            self.last_added_id = None
            self._save_stats()

    def compact(self):
        with self._lock:
            self.flush()
            if len(self.segments) < 2:
                pass
            print('Looseene: Starting smart compaction...', file=sys.stderr)
            all_docs = {}
            all_lens = {}
            new_inverted = defaultdict(list)
            merged_map = {}
            temp_docs_list = []
            for seg in self.segments:
                for doc_id in seg.doc_index:
                    d = seg.get_document(doc_id)
                    if d:
                        temp_docs_list.append((doc_id, d, seg.get_doc_len(doc_id)))
                seg.close()
            temp_docs_list.sort(key=lambda x: x[0])
            for doc_id, doc, doc_len in temp_docs_list:
                cmd = doc.get('inp', '').strip()
                h = self._get_cmd_hash(cmd)
                d_cnt = doc.get('cnt', 1)
                d_cmt = doc.get('cmt', '')
                if h not in merged_map:
                    merged_map[h] = {'doc_id': doc_id, 'doc': doc, 'len': doc_len, 'cnt': d_cnt, 'cmt': d_cmt}
                else:
                    merged_map[h]['cnt'] = max(merged_map[h]['cnt'], d_cnt)
                    if d_cmt:
                        merged_map[h]['cmt'] = d_cmt
                    merged_map[h]['doc_id'] = doc_id
                    merged_map[h]['doc'] = doc
                    merged_map[h]['len'] = doc_len
            self.seen_metadata = {}
            for h, meta in merged_map.items():
                doc_id = meta['doc_id']
                doc = meta['doc']
                doc['cnt'] = meta['cnt']
                doc['cmt'] = meta['cmt']
                self.seen_metadata[h] = {'cnt': meta['cnt'], 'cmt': meta['cmt']}
                all_docs[doc_id] = doc
                all_lens[doc_id] = meta['len']
                tokens = TextProcessor.process(doc.get('inp', ''))
                term_counts = Counter(tokens)
                for term, tf in term_counts.items():
                    new_inverted[term].append((doc_id, tf))
            new_seg_id = f'merged_{time.time_ns()}'
            SegmentWriter.write(self.path, new_seg_id, new_inverted, all_docs, all_lens)
            for seg in self.segments:
                if os.path.exists(seg.dir_path):
                    shutil.rmtree(seg.dir_path)
            self.segments = []
            self._load_segments()
            self._save_stats()
            print(f'Looseene: Compaction done. Unique docs: {len(all_docs)}', file=sys.stderr)

    def search(self, query: str, limit: int = 10) -> List[Dict]:
        query_tokens = TextProcessor.process(query)
        if not query_tokens:
            return []
        bm25 = BM25()
        avg_dl = self.stats['total_len'] / max(1, self.stats['total_docs'])
        scores = defaultdict(float)
        for q_term in query_tokens:
            expanded_terms = set()
            for mem_term in self.mem_inverted.keys():
                if mem_term.startswith(q_term):
                    expanded_terms.add(mem_term)
            for seg in self.segments:
                for seg_term in seg.vocab.keys():
                    if seg_term.startswith(q_term):
                        expanded_terms.add(seg_term)
            if not expanded_terms:
                expanded_terms.add(q_term)
            for term in expanded_terms:
                if term not in self.stats['doc_freqs']:
                    continue
                idf = math.log(
                    1
                    + (self.stats['total_docs'] - self.stats['doc_freqs'][term] + 0.5)
                    / (self.stats['doc_freqs'][term] + 0.5)
                )
                if term in self.mem_inverted:
                    for doc_id, tf in self.mem_inverted[term].items():
                        scores[doc_id] += bm25.score(tf, self.mem_doc_lens[doc_id], avg_dl, idf)
                for seg in self.segments:
                    for doc_id, tf in seg.get_postings(term):
                        scores[doc_id] += bm25.score(tf, seg.get_doc_len(doc_id), avg_dl, idf)
        candidate_limit = limit * 3
        top_ids = heapq.nlargest(candidate_limit, scores.keys(), key=lambda k: scores[k])
        results = []
        seen_hashes = set()
        for doc_id in top_ids:
            doc = None
            if doc_id in self.mem_docs:
                doc = self.mem_docs[doc_id]
            else:
                for seg in reversed(self.segments):
                    doc = seg.get_document(doc_id)
                    if doc:
                        break
            if doc:
                cmd = doc.get('inp', '').strip()
                h = self._get_cmd_hash(cmd)
                if h not in seen_hashes:
                    meta = self.seen_metadata.get(h)
                    if meta:
                        doc['cnt'] = meta['cnt']
                        doc['cmt'] = meta['cmt']
                    seen_hashes.add(h)
                    results.append(doc)
                    if len(results) >= limit:
                        break
        return results


class SearchEngineHistory(History):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sessionid = str(uuid.uuid4())
        xdg_data_home = os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share'))
        self.data_dir = os.path.join(xdg_data_home, 'xonsh', 'looseene_history')
        with _REGISTRY_LOCK:
            if 'xonsh_search' not in _REGISTRY:
                _REGISTRY['xonsh_search'] = IndexEngine('xonsh_search', self.data_dir)
            self.engine = _REGISTRY['xonsh_search']

    def append(self, cmd):
        doc = cmd.copy()
        doc['id'] = time.time_ns()
        doc['sessionid'] = self.sessionid
        doc.pop('out', None)
        self.engine.add(doc)
        try:
            self.engine.flush()
        except Exception as e:
            print(f'History Err: {e}', file=sys.stderr)

    def items(self, newest_first=False):
        all_docs = list(self.engine.mem_docs.values())
        for seg in self.engine.segments:
            for doc_id in seg.doc_index:
                d = seg.get_document(doc_id)
                if d:
                    all_docs.append(d)
        all_docs.sort(key=lambda x: x['id'], reverse=newest_first)
        seen_hashes = set()
        unique_docs = []
        for doc in all_docs:
            cmd = doc.get('inp', '').strip()
            h = hashlib.md5(cmd.encode('utf-8')).hexdigest()
            if h not in seen_hashes:
                meta = self.engine.seen_metadata.get(h)
                if meta:
                    doc['cnt'] = meta['cnt']
                    doc['cmt'] = meta['cmt']
                seen_hashes.add(h)
                unique_docs.append(doc)
        yield from unique_docs

    def all_items(self, newest_first=False):
        yield from self.items(newest_first)

    def info(self):
        data = collections.OrderedDict()
        data['backend'] = 'custom_search_engine'
        data['sessionid'] = self.sessionid
        data['location'] = self.data_dir
        data['docs_in_index'] = self.engine.stats['total_docs']
        data['segments_count'] = len(self.engine.segments)
        return data

    def search(self, query, limit=10):
        return self.engine.search(query, limit)

    def run_compaction(self):
        self.engine.compact()

    def update_comment(self, doc_to_update, comment):
        new_doc = doc_to_update.copy()
        new_doc['id'] = time.time_ns()
        new_doc['cmt'] = comment
        new_doc['cnt'] = doc_to_update.get('cnt', 1)
        self.engine.add(new_doc)
        self.engine.flush()
