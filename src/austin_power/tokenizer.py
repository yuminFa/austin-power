from __future__ import annotations

import re
from functools import lru_cache
from importlib.metadata import version

import apsw
import apsw.fts5

RULES_VERSION = 1
ASCII_RUN = re.compile(r"[A-Za-z0-9_]+(?:[./-][A-Za-z0-9_]+)*")
_SEP = re.compile(r"[_./-]+")
_KEEP = ("NN", "VV", "VA", "XR", "SH", "SL", "SN")

@lru_cache(maxsize=1)
def get_kiwi():
    from kiwipiepy import Kiwi
    return Kiwi()

def signature() -> str:
    return f"kiwi/{RULES_VERSION}/kiwipiepy-{version('kiwipiepy')}/model-{version('kiwipiepy_model')}"

def analyze(text: str, *, for_query: bool) -> list[tuple[int, int, tuple[str, ...]]]:
    out: list[tuple[int, int, tuple[str, ...]]] = []
    runs = [(m.start(), m.end()) for m in ASCII_RUN.finditer(text)]
    for s, e in runs:
        word = text[s:e].lower()
        toks = [word]
        if not for_query and _SEP.search(word):
            toks += [p for p in _SEP.split(word) if p and p != word]
        out.append((s, e, tuple(dict.fromkeys(toks))))
    if any(ord(ch) > 127 for ch in text):
        for t in get_kiwi().tokenize(text):
            s, e = t.start, t.start + t.len
            if e <= s or any(s < re_ and e > rs for rs, re_ in runs):
                continue
            if t.tag.startswith(_KEEP) and t.form.strip():
                out.append((s, e, (t.form.lower(),)))
    out.sort(key=lambda x: (x[0], x[1]))
    return out

def query_tokens(text: str) -> list[str]:
    return [toks[0] for _, _, toks in analyze(text, for_query=True)]

@apsw.fts5.StringTokenizer
def _kiwi_tokenizer(con, args):
    def tokenize(text: str, flags: int, locale):
        for s, e, toks in analyze(text, for_query=bool(flags & apsw.FTS5_TOKENIZE_QUERY)):
            yield (s, e, *toks)
    return tokenize

def register(conn: apsw.Connection) -> None:
    conn.register_fts5_tokenizer("kiwi", _kiwi_tokenizer)
