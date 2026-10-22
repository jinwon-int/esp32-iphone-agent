"""Korean text as the keys of the Korean 2-set (dubeolsik) layout, so the board's ASCII keyboard can type it (#17-E).

Once iOS recognizes the board as a hardware keyboard, it reads the keys with the current input language. In Korean
the 2-set layout composes syllables from these keys exactly as a Korean keyboard does, so typing the key sequence of
each syllable's jamo reproduces the text (the IME splits a final consonant off again when a vowel follows). Which
language is active is the phone's state: `key ctrl+space` switches it, and the agent checks the result on screen.

One run of keys is one language: Latin letters and Hangul cannot share a run, since the keys would come out in the
wrong alphabet. `script_runs` splits mixed text into Korean and English runs (digits, spaces and the usual symbols
are the same keys on both layouts and stay with the run they are in), and Phone.type switches the input language
between the runs. `to_dubeolsik` still refuses a mixed run.
"""

_BASE, _LAST = 0xAC00, 0xD7A3  # Hangul syllables 가..힣

# 2-set key of each single jamo (compatibility jamo); shifted keys are the double consonants and ㅒ ㅖ
_KEY = {
    "ㄱ": "r", "ㄲ": "R", "ㄴ": "s", "ㄷ": "e", "ㄸ": "E", "ㄹ": "f", "ㅁ": "a", "ㅂ": "q", "ㅃ": "Q", "ㅅ": "t",
    "ㅆ": "T", "ㅇ": "d", "ㅈ": "w", "ㅉ": "W", "ㅊ": "c", "ㅋ": "z", "ㅌ": "x", "ㅍ": "v", "ㅎ": "g",
    "ㅏ": "k", "ㅐ": "o", "ㅑ": "i", "ㅒ": "O", "ㅓ": "j", "ㅔ": "p", "ㅕ": "u", "ㅖ": "P", "ㅗ": "h", "ㅛ": "y",
    "ㅜ": "n", "ㅠ": "b", "ㅡ": "m", "ㅣ": "l",
    # compound vowels and finals are typed as their two parts
    "ㅘ": "hk", "ㅙ": "ho", "ㅚ": "hl", "ㅝ": "nj", "ㅞ": "np", "ㅟ": "nl", "ㅢ": "ml",
    "ㄳ": "rt", "ㄵ": "sw", "ㄶ": "sg", "ㄺ": "fr", "ㄻ": "fa", "ㄼ": "fq", "ㄽ": "ft", "ㄾ": "fx", "ㄿ": "fv",
    "ㅀ": "fg", "ㅄ": "qt",
}
_INITIAL = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_MEDIAL = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_FINAL = ["", *"ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"]


def _is_syllable(c: str) -> bool:
    return _BASE <= ord(c) <= _LAST


def has_hangul(text: str) -> bool:
    return any(_is_syllable(c) or c in _KEY for c in text)


def to_dubeolsik(text: str) -> str:
    """The 2-set keys that type `text` (Hangul, digits, spaces, symbols) with the Korean input language active."""
    out = []
    for c in text:
        if _is_syllable(c):
            i = ord(c) - _BASE
            out += [_KEY[_INITIAL[i // 588]], _KEY[_MEDIAL[i % 588 // 28]], _KEY.get(_FINAL[i % 28], "")]
        elif c in _KEY:
            out.append(_KEY[c])
        elif c.isascii() and c.isalpha():
            raise ValueError("type Korean and English separately: one typing uses one input language")
        elif c.isascii():
            out.append(c)
        else:
            raise ValueError(f"cannot type {c!r}: only Korean and ASCII text")
    return "".join(out)



_KEY_JAMO = {v: k for k, v in _KEY.items() if len(v) == 1}  # one key -> the jamo it types (shifted keys included)
_COMPOUND_VOWEL = {("ㅗ", "ㅏ"): "ㅘ", ("ㅗ", "ㅐ"): "ㅙ", ("ㅗ", "ㅣ"): "ㅚ", ("ㅜ", "ㅓ"): "ㅝ", ("ㅜ", "ㅔ"): "ㅞ",
                   ("ㅜ", "ㅣ"): "ㅟ", ("ㅡ", "ㅣ"): "ㅢ"}
_COMPOUND_FINAL = {("ㄱ", "ㅅ"): "ㄳ", ("ㄴ", "ㅈ"): "ㄵ", ("ㄴ", "ㅎ"): "ㄶ", ("ㄹ", "ㄱ"): "ㄺ", ("ㄹ", "ㅁ"): "ㄻ",
                   ("ㄹ", "ㅂ"): "ㄼ", ("ㄹ", "ㅅ"): "ㄽ", ("ㄹ", "ㅌ"): "ㄾ", ("ㄹ", "ㅍ"): "ㄿ", ("ㄹ", "ㅎ"): "ㅀ",
                   ("ㅂ", "ㅅ"): "ㅄ"}
_SPLIT_FINAL = {v: k for k, v in _COMPOUND_FINAL.items()}


def compose_dubeolsik(keys: str) -> str:
    """What the Korean 2-set IME shows when `keys` are typed with the Korean input language active — the reverse of
    `to_dubeolsik`, and what English text becomes when the phone was left in Korean ("Safari" -> "ㄴㅁㄹㅁ갸", device
    2026-10-22). Composes like the IME: an initial, then a medial (compound vowels from two keys), then a final
    (compound finals from two keys); a vowel after a final takes that final (its second part) as the next initial; a
    consonant that cannot continue the syllable starts a new one; jamo that never meet a partner stay as they are.
    Shifted keys without a double consonant type the plain one (S -> ㄴ); other characters pass through."""
    out: list[str] = []
    ini = med = fin = None

    def flush() -> None:
        nonlocal ini, med, fin
        if ini and med:
            out.append(chr(_BASE + _INITIAL.index(ini) * 588 + _MEDIAL.index(med) * 28 + _FINAL.index(fin or "")))
        else:
            out.extend(j for j in (ini, med) if j)
        ini = med = fin = None

    for ch in keys:
        jamo = _KEY_JAMO.get(ch) or (_KEY_JAMO.get(ch.lower()) if ch.isascii() and ch.isalpha() else None)
        if jamo is None:
            flush()
            out.append(ch)
        elif jamo in _MEDIAL:
            if ini and not med:
                med = jamo
            elif med and not fin and (med, jamo) in _COMPOUND_VOWEL:
                med = _COMPOUND_VOWEL[(med, jamo)]
            elif ini and med and fin:
                kept, carry = _SPLIT_FINAL.get(fin, ("", fin))
                fin = kept or None
                flush()
                ini, med = carry, jamo
            else:
                flush()
                med = jamo
        else:
            if ini is None and med is None:
                ini = jamo
            elif ini and med and not fin and jamo in _FINAL:
                fin = jamo
            elif ini and med and fin and (fin, jamo) in _COMPOUND_FINAL:
                fin = _COMPOUND_FINAL[(fin, jamo)]
            else:
                flush()
                ini = jamo
    flush()
    return "".join(out)


def script_runs(text: str) -> list[tuple[bool, str]]:
    """`text` as (korean, run) pieces: a run changes where a Hangul character follows a Latin letter or the reverse.
    Digits, spaces and symbols belong to the run they are in; leading ones go with the first letter that follows
    (or make one non-Korean run when there is no letter at all)."""
    runs: list[list] = []  # [korean | None, chars]
    for c in text:
        korean = True if (_is_syllable(c) or c in _KEY) else (False if (c.isascii() and c.isalpha()) else None)
        if not runs or (korean is not None and runs[-1][0] is not None and runs[-1][0] != korean):
            runs.append([korean, c])
        else:
            runs[-1][1] += c
            if runs[-1][0] is None:
                runs[-1][0] = korean
    return [(bool(k), r) for k, r in runs]
