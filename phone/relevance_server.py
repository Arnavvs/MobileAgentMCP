#!/data/data/com.termux/files/usr/bin/python
"""On-device relevance scoring for feed shaping. Runs in Termux on the phone.

Why this exists: `feed.read.tag()` is a regex, and a regex cannot judge whether
a post is about the thing you asked for. It has already called Basecamp
political (`mp\\b` inside "Basecamp") and "nuclear arsenals" football
(`arsenal`). Those are not tuning problems - keyword matching has no notion of
meaning. Embeddings do.

Why static embeddings rather than a transformer: this has to run on the phone,
on CPU, fast enough to sit inside a scrolling loop. Model2Vec's potion-base-8M
is a distilled STATIC embedding table - no attention, no forward pass, just a
vocabulary lookup and a mean. It reaches roughly 90% of all-MiniLM-L6-v2's
quality at a fraction of the cost, which is the right trade when the
alternative is a regex.

Why no dependencies beyond numpy: the `model2vec` package pulls in `tokenizers`,
a Rust extension with no aarch64-Android wheel, so installing it on Termux means
a Rust toolchain and a long build. Everything needed here is small enough to
implement directly - safetensors is a JSON header followed by raw bytes, and
potion uses an ordinary BERT WordPiece vocabulary. So this file reads the model
itself and tokenizes itself, and the only import is numpy.

Serves HTTP on 127.0.0.1 so the laptop can reach it with:
    adb forward tcp:8765 tcp:8765

    POST /score  {"query": "bollywood gossip", "texts": ["...", "..."]}
      -> {"scores": [0.42, 0.08], "ms": 3}
    GET  /health -> {"ok": true, "vocab": 29528, "dim": 256}
"""

import json
import re
import struct
import sys
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MODEL_DIR = HERE / "potion-base-8M"


# --------------------------------------------------------------------------
# safetensors, by hand
# --------------------------------------------------------------------------

_DTYPES = {"F32": np.float32, "F16": np.float16, "BF16": np.float32,
           "I64": np.int64, "I32": np.int32, "U8": np.uint8}


def load_safetensors(path: Path) -> dict:
    """Read a .safetensors file without the safetensors package.

    Layout: 8-byte little-endian header length, that many bytes of JSON
    describing every tensor (dtype, shape, byte range), then the raw data.
    """
    raw = path.read_bytes()
    n = struct.unpack("<Q", raw[:8])[0]
    header = json.loads(raw[8:8 + n])
    base = 8 + n
    out = {}
    for name, meta in header.items():
        if name == "__metadata__":
            continue
        dt = meta["dtype"]
        if dt == "BF16":
            # bfloat16 is the top 16 bits of a float32 - widen rather than fail
            start, end = meta["data_offsets"]
            u16 = np.frombuffer(raw[base + start:base + end], dtype=np.uint16)
            u32 = u16.astype(np.uint32) << 16
            arr = u32.view(np.float32)
        else:
            if dt not in _DTYPES:
                raise ValueError("unsupported dtype %s for %s" % (dt, name))
            start, end = meta["data_offsets"]
            arr = np.frombuffer(raw[base + start:base + end], dtype=_DTYPES[dt])
        out[name] = arr.reshape(meta["shape"])
    return out


# --------------------------------------------------------------------------
# BERT WordPiece, by hand
# --------------------------------------------------------------------------

_PUNCT = re.compile(r"([^\w\s]|_)", re.UNICODE)

_URL = re.compile(r"https?://\S+|\b\w+\.(?:com|org|net|co|in|io)/\S*", re.I)
_HASHTAG = re.compile(r"#(\w+)")
_MENTION = re.compile(r"@\w+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def normalise(text: str) -> str:
    """Turn social-media text into something a word tokenizer can read.

    Hashtags are the big one. "#ShahidKapoor" is one unknown word that shatters
    into meaningless subwords, and mean pooling then buries the signal: the very
    same post scored 0.097 against "bollywood film gossip" written with hashtags
    and 0.316 with plain words. Splitting on camel case recovers it.

    URLs and @mentions contribute nothing but noise to a topical judgement -
    every character of them is averaged into the vector - so they go.
    """
    text = _URL.sub(" ", text or "")
    text = _MENTION.sub(" ", text)
    text = _HASHTAG.sub(lambda m: " " + _CAMEL.sub(" ", m.group(1)) + " ", text)
    return text


class WordPiece:
    """Greedy longest-match-first WordPiece, the BERT tokenizer's core.

    Enough of the real thing to be faithful for this purpose: lowercase, strip
    accents, split punctuation, then match the longest vocabulary entry at each
    position with `##` continuations. Unknown words map to [UNK], which the
    scorer then drops rather than embedding as noise.
    """

    def __init__(self, vocab: dict, unk: str = "[UNK]", lower: bool = True):
        self.vocab = vocab
        self.unk = unk
        self.lower = lower
        self.max_len = max(len(t) for t in vocab) if vocab else 1

    def _basic(self, text: str) -> list:
        if self.lower:
            text = text.lower()
        text = unicodedata.normalize("NFD", text)
        text = "".join(c for c in text if unicodedata.category(c) != "Mn")
        text = _PUNCT.sub(r" \1 ", text)
        return text.split()

    def encode(self, text: str) -> list:
        ids = []
        for word in self._basic(text):
            if len(word) > 100:
                ids.append(self.vocab.get(self.unk, 0))
                continue
            start, pieces, ok = 0, [], True
            while start < len(word):
                end = min(len(word), start + self.max_len)
                found = None
                while end > start:
                    sub = word[start:end]
                    if start > 0:
                        sub = "##" + sub
                    if sub in self.vocab:
                        found = sub
                        break
                    end -= 1
                if found is None:
                    ok = False
                    break
                pieces.append(self.vocab[found])
                start = end
            ids.extend(pieces if ok else [self.vocab.get(self.unk, 0)])
        return ids


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

class Embedder:
    def __init__(self, model_dir: Path):
        vocab_file = model_dir / "vocab.txt"
        tokens = vocab_file.read_text(encoding="utf-8").splitlines()
        self.vocab = {t: i for i, t in enumerate(tokens)}
        self.unk_id = self.vocab.get("[UNK]", 0)

        tensors = load_safetensors(model_dir / "model.safetensors")
        # the table is whichever 2-D tensor has one row per vocabulary entry
        emb = None
        for name, arr in tensors.items():
            if arr.ndim == 2 and arr.shape[0] >= len(tokens) - 8:
                emb = arr
                break
        if emb is None:
            raise SystemExit("no embedding matrix in %s (saw %s)"
                             % (model_dir, {k: v.shape for k, v in tensors.items()}))
        self.emb = np.asarray(emb, dtype=np.float32)
        self.dim = int(self.emb.shape[1])
        self.tok = WordPiece(self.vocab)
        # tokens carrying no meaning on their own drag every vector toward the
        # centre of the space, which flattens the differences we care about
        self.skip = {self.unk_id}
        for special in ("[CLS]", "[SEP]", "[PAD]", "[MASK]"):
            if special in self.vocab:
                self.skip.add(self.vocab[special])

    def encode(self, text: str) -> np.ndarray:
        ids = [i for i in self.tok.encode(normalise(text)) if i not in self.skip]
        if not ids:
            return np.zeros(self.dim, dtype=np.float32)
        v = self.emb[ids].mean(axis=0)      # static embeddings: mean pooling
        n = np.linalg.norm(v)
        return v / n if n else v

    def encode_many(self, texts: list) -> np.ndarray:
        return np.vstack([self.encode(t) for t in texts]) if texts \
            else np.zeros((0, self.dim), dtype=np.float32)


EMB: Embedder | None = None


def score(query: str, texts: list) -> list:
    q = EMB.encode(query)
    M = EMB.encode_many(texts)
    if not len(M):
        return []
    return [round(float(x), 4) for x in M @ q]      # both sides normalised


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send({"ok": True, "vocab": len(EMB.vocab), "dim": EMB.dim})
        else:
            self._send({"error": "POST /score or GET /health"}, 404)

    def do_POST(self):
        if not self.path.startswith("/score"):
            return self._send({"error": "unknown path"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            t0 = time.time()
            out = score(req.get("query") or "", req.get("texts") or [])
            self._send({"scores": out, "ms": int((time.time() - t0) * 1000)})
        except Exception as e:
            self._send({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def log_message(self, *a):
        pass        # the default logger writes a line per request to stderr


def main() -> int:
    global EMB
    t0 = time.time()
    EMB = Embedder(MODEL_DIR)
    load_ms = int((time.time() - t0) * 1000)

    if "--selftest" in sys.argv:
        q = "bollywood film gossip"
        texts = ["Toxic advance booking record for Yash",
                 "Arsenal beat Chelsea 2-0 at the Emirates",
                 "Nuclear-armed states ramped up arsenals in 2025",
                 "Started & runs 37signals, makers of Basecamp",
                 "CONFIRMED: #ShahidKapoor and #AliaBhatt in #DRAGON",
                 "Letssss Goooo #BiggBoss20 get ready"]
        t1 = time.time()
        s = score(q, texts)
        print(json.dumps({"loaded_ms": load_ms, "dim": EMB.dim,
                          "vocab": len(EMB.vocab),
                          "score_ms": int((time.time() - t1) * 1000),
                          "query": q,
                          "scores": dict(zip([t[:38] for t in texts], s))},
                         indent=1))
        return 0

    port = 8765
    print("relevance server: vocab=%d dim=%d loaded_ms=%d port=%d"
          % (len(EMB.vocab), EMB.dim, load_ms, port), flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
