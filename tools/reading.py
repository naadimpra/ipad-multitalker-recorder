"""Draft readings for a Japanese transcript line: text -> yomigana (hiragana) -> romaji.

    python -m tools.reading "今日は東京へ行きます。"
    きょう は とうきょう へ いきます。
    kyou wa toukyou e ikimasu.

Used by the transcription page (tools.annotate) to fill the yomigana and romaji fields, which the
annotator then corrects. Yomigana is written with a space between words so it lines up with the romaji.

  text -> yomigana   needs a dictionary: pip install fugashi unidic-lite  (optional; without it the
                     yomigana is typed by hand and only the romaji is drafted)
  yomigana -> romaji Hepburn as typed on a keyboard: long vowels stay spelled out (toukyou, not tōkyō),
                     ん before a vowel is n', っ doubles the next consonant. The particles は / へ / を
                     become wa / e / o. A ＊ (or *) after a mora marks its vowel as devoiced, written
                     as a capital in the romaji: し＊た -> shIta, です＊ -> desU.
"""
from __future__ import annotations

import re
import threading

# ---------------- kana -> romaji ----------------
_K: dict[str, str] = {}
for _c, _row in {"": "あいうえお", "k": "かきくけこ", "s": "さしすせそ", "t": "たちつてと", "n": "なにぬねの",
                 "h": "はひふへほ", "m": "まみむめも", "r": "らりるれろ", "g": "がぎぐげご", "z": "ざじずぜぞ",
                 "d": "だぢづでど", "b": "ばびぶべぼ", "p": "ぱぴぷぺぽ"}.items():
    for _k, _v in zip(_row, "aiueo"):
        _K[_k] = _c + _v
_K.update({"し": "shi", "ち": "chi", "つ": "tsu", "ふ": "fu", "じ": "ji", "ぢ": "ji", "づ": "zu",
           "や": "ya", "ゆ": "yu", "よ": "yo", "わ": "wa", "ゐ": "i", "ゑ": "e", "を": "o", "ゔ": "vu",
           "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o", "ゃ": "ya", "ゅ": "yu", "ょ": "yo", "ゎ": "wa"})
for _k, _c in {"き": "ky", "ぎ": "gy", "に": "ny", "ひ": "hy", "び": "by", "ぴ": "py", "み": "my", "り": "ry",
               "し": "sh", "ち": "ch", "じ": "j", "ぢ": "j"}.items():
    for _s, _v in zip("ゃゅょ", "auo"):
        _K[_k + _s] = _c + _v
_K.update({"しぇ": "she", "じぇ": "je", "ちぇ": "che", "つぁ": "tsa", "つぃ": "tsi", "つぇ": "tse", "つぉ": "tso",
           "てぃ": "ti", "でぃ": "di", "てゅ": "tyu", "でゅ": "dyu", "とぅ": "tu", "どぅ": "du",
           "ふぁ": "fa", "ふぃ": "fi", "ふぇ": "fe", "ふぉ": "fo", "ふゅ": "fyu", "いぇ": "ye",
           "うぃ": "wi", "うぇ": "we", "うぉ": "wo", "くぁ": "kwa", "ぐぁ": "gwa",
           "ゔぁ": "va", "ゔぃ": "vi", "ゔぇ": "ve", "ゔぉ": "vo"})
_PUNCT = {"、": ",", "。": ".", "！": "!", "？": "?", "「": '"', "」": '"', "『": '"', "』": '"', "・": " ",
          "〜": "~", "～": "~", "…": "...", "　": " ", "，": ",", "．": "."}
_PARTICLES = {"は": "wa", "へ": "e", "を": "o"}
DEVOICED = "＊*"  # typed after a mora in the yomigana


def to_hiragana(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _syllable(s: str, i: int) -> tuple[str | None, int]:
    if s[i:i + 2] in _K:
        return _K[s[i:i + 2]], 2
    return _K.get(s[i]), 1


def _word(kana: str) -> str:
    s, out, i = to_hiragana(kana), "", 0
    s = s.replace("こんにちは", "こんにちわ").replace("こんばんは", "こんばんわ")  # greetings end in the particle は
    while i < len(s):
        c = s[i]
        if c == "っ":  # double the consonant that follows (っち -> tchi)
            nxt = _syllable(s, i + 1)[0] if i + 1 < len(s) else None
            if nxt and nxt[0] not in "aiueo":
                out += "t" if nxt.startswith("ch") else nxt[0]
            i += 1
        elif c == "ん":
            nxt = _syllable(s, i + 1)[0] if i + 1 < len(s) else None
            out += "n'" if nxt and nxt[0] in "aiueoy" else "n"
            i += 1
        elif c == "ー":  # long vowel mark: repeat the vowel before it
            out += out[-1].lower() if out and out[-1] in "aiueoAIUEO" else "-"
            i += 1
        elif c in DEVOICED:  # the mora before it has a devoiced vowel: capital letter
            if out and out[-1] in "aiueo":
                out = out[:-1] + out[-1].upper()
            i += 1
        else:
            r, n = _syllable(s, i)
            out += r if r is not None else _PUNCT.get(c, c)
            i += n
    return out


def romaji(kana: str) -> str:
    """Hepburn for yomigana written with spaces between words. A lone は / へ / を is read as a particle."""
    words = []
    for w in kana.split():
        m = re.fullmatch(r"([はへを])([、。！？,.!?]*)", w)
        words.append(_PARTICLES[m[1]] + _word(m[2]) if m else _word(w))
    return re.sub(r"\s+", " ", " ".join(words)).strip()


# ---------------- text -> yomigana (needs fugashi + a UniDic dictionary) ----------------
_tagger = None
_lock = threading.Lock()
_GLUE_PARTICLES = {"て", "で", "ば", "たり", "だり", "ちゃ", "じゃ"}  # 降って, 行けば: one word in romaji
_OPENERS = set("「『（(［[")


def available() -> bool:
    global _tagger
    if _tagger is None:
        try:
            import fugashi
            _tagger = fugashi.Tagger()
        except Exception:  # not installed, or no dictionary
            _tagger = False
    return bool(_tagger)


def readings(text: str) -> dict:
    """{"kana": yomigana or None when no dictionary is installed, "romaji": romaji of that yomigana}."""
    if not text.strip():
        return {"kana": "", "romaji": ""}
    if not available():
        return {"kana": None, "romaji": None}
    with _lock:
        tokens = [(w.surface, w.feature) for w in _tagger(text)]
    chunks: list[list] = []   # [kana, particle romaji or None]; a chunk is one written word
    glue_next = False         # the next token continues the current chunk (after お / ご, or an opening bracket)
    for surface, f in tokens:
        pos1, pos2 = getattr(f, "pos1", ""), getattr(f, "pos2", "")
        if pos1 in ("補助記号", "空白"):
            if surface in _OPENERS or not chunks:
                chunks.append([surface, None])
                glue_next = True
            else:
                chunks[-1][0] += surface
            continue
        kana = getattr(f, "kana", None)
        kana = to_hiragana(kana if kana and kana != "*" else surface)  # no reading (digits, unknown words): keep as typed
        particle = pos1 == "助詞" and surface in _PARTICLES
        attach = glue_next or pos1 == "接尾辞" \
            or (pos1 == "助動詞" and getattr(f, "lemma", "") not in ("です", "だ")) \
            or (pos1 == "助詞" and pos2 == "接続助詞" and surface in _GLUE_PARTICLES)
        glue_next = pos1 == "接頭辞"
        if attach and chunks and not particle:
            chunks[-1][0] += kana
        else:
            chunks.append([kana, _PARTICLES[surface] if particle else None])
    # Romaji per whole word, so っ / ん see what follows. A particle keeps its reading (は -> wa);
    # anything after its first character is punctuation that was attached to it.
    roma = " ".join(p + _word(k[1:]) if p else _word(k) for k, p in chunks)
    return {"kana": " ".join(k for k, _ in chunks), "romaji": re.sub(r"\s+", " ", roma).strip()}


if __name__ == "__main__":
    import sys
    for line in sys.argv[1:]:
        r = readings(line)
        if r["kana"] is None:
            raise SystemExit("No dictionary: pip install fugashi unidic-lite")
        print(r["kana"], r["romaji"], sep="\n")
