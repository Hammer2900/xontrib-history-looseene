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
from pathlib import Path
from collections import defaultdict, Counter
from itertools import chain
from typing import Dict, List, Tuple, Optional, Any, Iterator

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

    @classmethod
    def stem(cls, word: str) -> str:
        """Applies simple stemming rules to a word."""
        return word if len(word) < 4 or (word.endswith('ing') and len(word) < 5) else cls.SUFFIXES.sub('', word)

    @classmethod
    def process(cls, text: str) -> List[str]:
        """Tokenizes and stems text using iterators."""
        return list(map(cls.stem, cls.TOKEN_RE.findall(text.lower()))) if text else []


class BM25:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b

    def score(self, tf: int, doc_len: int, avg_dl: float, idf: float) -> float:
        """Calculates BM25 score."""
        return idf * ((tf * (self.k1 + 1)) / (tf + self.k1 * (1 - self.b + self.b * (doc_len / avg_dl))))


class DiskSegment:
    def __init__(self, dir_path: Path):
        self.path = dir_path
        self.vocab: Dict[str, Tuple[int, int]] = {}
        self.doc_index: Dict[int, Tuple[int, int, int]] = {}
        self.maps: Dict[str, mmap.mmap] = {}
        self.files: Dict[str, Any] = {}
        try:
            self._init_resources()
            self.vocab = (
                json.loads((self.path / 'vocab.json').read_text('utf-8')) if (self.path / 'vocab.json').exists() else {}
            )
            idx = json.loads((self.path / 'doc_idx.json').read_text()) if (self.path / 'doc_idx.json').exists() else {}
            self.doc_index = {int(k): tuple(v) for k, v in idx.items()}
        except Exception:
            self.close()
            raise

    def _init_resources(self):
        """Opens files and mmaps resources safely."""
        for name in ('postings', 'docs'):
            p = self.path / f'{name}.bin'
            if not p.exists():
                p.touch()
            try:
                f = p.open('rb')
                self.files[name] = f
                if p.stat().st_size > 0:
                    self.maps[name] = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            except Exception as e:
                self.close()
                raise RuntimeError(f'Failed to initialize {name}: {e}')

    def get_postings(self, term: str) -> Iterator[Tuple[int, int]]:
        """Yields delta-decoded postings."""
        if term not in self.vocab or 'postings' not in self.maps:
            return iter([])
        offset, length = self.vocab[term]
        try:
            raw = zlib.decompress(self.maps['postings'][offset : offset + length])

            def _iter():
                last = 0
                for d, tf in POSTING_STRUCT.iter_unpack(raw):
                    last += d
                    yield last, tf

            return _iter()
        except Exception:
            return iter([])

    def get_document(self, doc_id: int) -> Optional[Dict]:
        """Retrieves document from compressed store."""
        if doc_id not in self.doc_index or 'docs' not in self.maps:
            return None
        offset, length, _ = self.doc_index[doc_id]
        try:
            return json.loads(zlib.decompress(self.maps['docs'][offset : offset + length]).decode('utf-8'))
        except Exception:
            return None

    def get_doc_len(self, doc_id: int) -> int:
        return self.doc_index.get(doc_id, (0, 0, 0))[2]

    def close(self):
        """Closes all file handles and mmaps."""
        for m in self.maps.values():
            m.close()
        for f in self.files.values():
            f.close()
        self.maps.clear()
        self.files.clear()


class SegmentWriter:
    @staticmethod
    def write(base_dir: Path, seg_id: str, inverted: Dict, docs: Dict, doc_lens: Dict) -> Path:
        """Writes index to disk with delta-encoding."""
        seg_dir = base_dir / f'seg_{seg_id}'
        seg_dir.mkdir(parents=True, exist_ok=True)
        vocab, doc_idx = {}, {}
        with (seg_dir / 'postings.bin').open('wb') as fp:
            curr = 0
            for term, posts in sorted(inverted.items()):
                last, buf = 0, bytearray()
                for did, tf in sorted(posts):
                    buf.extend(POSTING_STRUCT.pack(did - last, tf))
                    last = did
                comp = zlib.compress(buf)
                fp.write(comp)
                vocab[term] = (curr, len(comp))
                curr += len(comp)
        with (seg_dir / 'docs.bin').open('wb') as fd:
            curr = 0
            for did, data in docs.items():
                comp = zlib.compress(json.dumps(data).encode('utf-8'))
                fd.write(comp)
                doc_idx[did] = (curr, len(comp), doc_lens.get(did, 0))
                curr += len(comp)
        (seg_dir / 'vocab.json').write_text(json.dumps(vocab), 'utf-8')
        (seg_dir / 'doc_idx.json').write_text(json.dumps(doc_idx))
        return seg_dir


class IndexEngine:
    def __init__(self, name: str, path: str):
        self.path = Path(path)
        self.mem = {'docs': {}, 'lens': {}, 'inv': defaultdict(lambda: defaultdict(int))}
        self.stats = {'total_docs': 0, 'total_len': 0, 'doc_freqs': Counter()}
        self.seen_meta: Dict[str, Dict] = {}
        self.last = {'hash': None, 'id': None}
        self.segments: List[DiskSegment] = []
        self._lock = threading.RLock()
        if self.path.exists():
            self._load_stats()
            self.segments = [DiskSegment(p) for p in sorted(self.path.glob('seg_*'))]
        else:
            self.path.mkdir(parents=True, exist_ok=True)

    def _load_stats(self):
        try:
            d = json.loads((self.path / 'stats.json').read_text())
            self.stats.update({k: v for k, v in d.items() if k != 'doc_freqs'})
            self.stats['doc_freqs'] = Counter(d.get('doc_freqs', {}))
            hashes = d.get('seen_hashes', [])
            self.seen_meta = {h: {'cnt': 1, 'cmt': ''} for h in hashes} if isinstance(hashes, list) else hashes
        except Exception:
            pass

    def _save_stats(self):
        (self.path / 'stats.json').write_text(json.dumps({**self.stats, 'seen_hashes': self.seen_meta}))

    def add(self, doc: Dict):
        """Adds doc to memory buffer."""
        cmd = doc.get('inp', '').strip()
        if not cmd:
            return
        h = hashlib.md5(cmd.encode('utf-8')).hexdigest()
        with self._lock:
            meta = self.seen_meta.setdefault(h, {'cnt': 0, 'cmt': ''})
            doc['cnt'] = doc.get('cnt', meta['cnt'] + 1)
            doc['cmt'] = doc.get('cmt', meta['cmt'])
            self.seen_meta[h] = {'cnt': doc['cnt'], 'cmt': doc['cmt']}
            if self.last['hash'] == h and self.last['id'] in self.mem['docs']:
                self.mem['docs'][self.last['id']].update({'cnt': doc['cnt'], 'cmt': doc['cmt']})
                return
            self.mem['docs'][doc['id']] = doc
            self.last.update({'hash': h, 'id': doc['id']})
            tokens = TextProcessor.process(cmd)
            self.mem['lens'][doc['id']] = len(tokens)
            self.stats['total_docs'] += 1
            self.stats['total_len'] += len(tokens)
            for t in tokens:
                self.mem['inv'][t][doc['id']] += 1
                self.stats['doc_freqs'][t] += 1

    def flush(self):
        """Writes memory buffer to disk."""
        with self._lock:
            if not self.mem['docs']:
                return
            inv_list = {t: list(d.items()) for t, d in self.mem['inv'].items()}
            path = SegmentWriter.write(self.path, str(time.time_ns()), inv_list, self.mem['docs'], self.mem['lens'])
            self.segments.append(DiskSegment(path))
            for k in self.mem:
                self.mem[k].clear()
            self.last.update({'hash': None, 'id': None})
            self._save_stats()

    def compact(self):
        """Merges segments, deduplicates, and cleans up resources."""
        with self._lock:
            self.flush()
            if len(self.segments) < 2:
                return
            all_data = []
            for s in self.segments:
                for did in s.doc_index:
                    doc = s.get_document(did)
                    if doc:
                        all_data.append((did, doc, s.get_doc_len(did)))
            merged, new_inv, all_docs, all_lens = {}, defaultdict(list), {}, {}
            for s in self.segments:
                s.close()
            for doc_id, doc, d_len in sorted(all_data, key=lambda x: x[0]):
                h = hashlib.md5(doc.get('inp', '').strip().encode('utf-8')).hexdigest()
                curr = merged.get(h, {'cnt': 0})
                merged[h] = {
                    'id': doc_id,
                    'doc': doc,
                    'len': d_len,
                    'cnt': max(curr['cnt'], doc.get('cnt', 1)),
                    'cmt': doc.get('cmt') or curr.get('cmt', ''),
                }
            self.seen_meta.clear()
            self.stats['total_docs'] = len(merged)
            self.stats['total_len'] = sum(m['len'] for m in merged.values())
            self.stats['doc_freqs'] = Counter()
            for h, m in merged.items():
                m['doc'].update({'cnt': m['cnt'], 'cmt': m['cmt']})
                self.seen_meta[h] = {'cnt': m['cnt'], 'cmt': m['cmt']}
                all_docs[m['id']] = m['doc']
                all_lens[m['id']] = m['len']
                for t, tf in Counter(TextProcessor.process(m['doc'].get('inp', ''))).items():
                    new_inv[t].append((m['id'], tf))
                    self.stats['doc_freqs'][t] += 1
            new_seg_path = SegmentWriter.write(self.path, f'merged_{time.time_ns()}', new_inv, all_docs, all_lens)
            for s in self.segments:
                shutil.rmtree(s.path)
            self.segments = [DiskSegment(new_seg_path)]
            self._save_stats()

    def search(self, query: str, limit: int = 10) -> List[Dict]:
        """BM25 Search."""
        tokens = TextProcessor.process(query)
        if not tokens:
            return []
        bm25, scores = BM25(), defaultdict(float)
        avg_dl = self.stats['total_len'] / max(1, self.stats['total_docs'])
        for q in tokens:
            terms = {t for t in self.mem['inv'] if t.startswith(q)} | {
                t for s in self.segments for t in s.vocab if t.startswith(q)
            }
            if not terms:
                terms = {q}
            for t in terms:
                if t not in self.stats['doc_freqs']:
                    continue
                idf = math.log(
                    1
                    + (self.stats['total_docs'] - self.stats['doc_freqs'][t] + 0.5) / (self.stats['doc_freqs'][t] + 0.5)
                )
                for did, tf in self.mem['inv'][t].items():
                    scores[did] += bm25.score(tf, self.mem['lens'][did], avg_dl, idf)
                for s in self.segments:
                    for did, tf in s.get_postings(t):
                        scores[did] += bm25.score(tf, s.get_doc_len(did), avg_dl, idf)
        results, seen = [], set()
        for did in heapq.nlargest(limit * 3, scores, key=scores.get):
            doc = self.mem['docs'].get(did) or next(
                (d for s in reversed(self.segments) if (d := s.get_document(did))), None
            )
            if doc:
                h = hashlib.md5(doc.get('inp', '').strip().encode('utf-8')).hexdigest()
                if h not in seen:
                    if meta := self.seen_meta.get(h):
                        doc.update(meta)
                    seen.add(h)
                    results.append(doc)
                    if len(results) >= limit:
                        break
        return results


class SearchEngineHistory(History):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.sessionid = str(uuid.uuid4())
        path = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local/share')) / 'xonsh' / 'looseene_history'
        with _REGISTRY_LOCK:
            self.engine = _REGISTRY.setdefault('xonsh_search', IndexEngine('xonsh_search', str(path)))

    def append(self, cmd: Dict):
        self.engine.add({**cmd, 'id': time.time_ns(), 'sessionid': self.sessionid, 'out': None})
        try:
            self.engine.flush()
        except Exception as e:
            print(f'History Err: {e}', file=sys.stderr)

    def items(self, newest_first=False) -> Iterator[Dict]:
        src = chain(
            self.engine.mem['docs'].values(),
            (d for s in self.engine.segments for did in s.doc_index if (d := s.get_document(did))),
        )
        all_docs = sorted(src, key=lambda x: x['id'], reverse=newest_first)
        seen = set()
        for doc in all_docs:
            h = hashlib.md5(doc.get('inp', '').strip().encode('utf-8')).hexdigest()
            if h not in seen:
                if meta := self.engine.seen_meta.get(h):
                    doc.update(meta)
                seen.add(h)
                yield doc

    def all_items(self, newest_first=False):
        return self.items(newest_first)

    def info(self):
        return {
            'backend': 'looseene',
            'sessionid': self.sessionid,
            'location': str(self.engine.path),
            'docs': self.engine.stats['total_docs'],
            'segments': len(self.engine.segments),
        }

    def search(self, query, limit=10):
        return self.engine.search(query, limit)

    def run_compaction(self):
        self.engine.compact()

    def update_comment(self, doc, comment):
        self.engine.add({**doc, 'id': time.time_ns(), 'cmt': comment})
        self.engine.flush()
