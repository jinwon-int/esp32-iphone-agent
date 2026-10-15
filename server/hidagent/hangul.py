"""Korean text as the keys of the Korean 2-set (dubeolsik) layout, so the board's ASCII keyboard can type it (#17-E).

Once iOS recognizes the board as a hardware keyboard, it reads the keys with the current input language. In Korean
the 2-set layout composes syllables from these keys exactly as a Korean keyboard does, so typing the key sequence of
each syllable's jamo reproduces the text (the IME splits a final consonant off again when a vowel follows). Which
language is active is the phone's state: `key ctrl+space` switches it, and the agent checks the result on screen.

One typing is one language: Latin letters mixed with Hangul would come out in the wrong alphabet, so that is refused.
Digits, spaces and the usual symbols are the same keys on both layouts and pass through.
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
