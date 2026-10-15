"""Korean text is typed through the board as the keys of the Korean 2-set (dubeolsik) layout (#17-E).

With the board recognized as a hardware keyboard, iOS reads its keys with the current input language; in Korean
the 2-set layout composes syllables from these keys exactly like a Korean keyboard.
"""

import asyncio

import pytest

from hidagent.device import DeviceError
from hidagent.hangul import has_hangul, to_dubeolsik


@pytest.mark.parametrize("text, keys", [
    ("설정", "tjfwjd"),                    # device-verified 2026-10-15: these keys composed "설정"
    ("갈비", "rkfql"),                     # final + next initial: the IME splits them back correctly
    ("괜찮아", "rhoscksgdk"),              # compound vowel (ㅙ) and compound final (ㄶ)
    ("테스트플라이트", "xptmxmvmffkdlxm"),  # 플(vmf) + 라(fk): ㄹㄹ is no compound final, so 라 starts anew
    ("까치 꽃 쌀", "Rkcl Rhc Tkf"),        # double consonants are the shifted keys
    ("얘기 예", "dOrl dP"),                # ㅒ ㅖ are shifted too
    ("값", "rkqt"),                        # ㅄ
    ("카톡 123!", "zkxhr 123!"),           # digits, spaces and symbols are the same on both layouts
    ("ㅋㅋ ㅠ", "zz b"),                   # bare compatibility jamo
])
def test_hangul_becomes_dubeolsik_keys(text, keys):
    assert to_dubeolsik(text) == keys


def test_latin_letters_cannot_share_one_typing_with_korean():
    # one typing has one input language; English letters would come out as Korean (and the reverse)
    with pytest.raises(ValueError, match="separately"):
        to_dubeolsik("설정 Wi-Fi")


def test_has_hangul():
    assert has_hangul("설정") and has_hangul("ㅋ") and not has_hangul("Settings 123")


class FakeDevice:
    def __init__(self):
        self.sent = []

    async def send(self, command):
        self.sent.append(command)


def make_phone(tmp_path):
    from hidagent.phone import Phone
    return Phone(FakeDevice(), tmp_path / "calibration.json", pointer="absolute")


def test_phone_types_korean_as_dubeolsik_keys(tmp_path):
    phone = make_phone(tmp_path)
    asyncio.run(phone.type("설정"))
    assert phone.device.sent == ["type tjfwjd"]


def test_phone_still_types_english_as_is_and_refuses_other_scripts(tmp_path):
    phone = make_phone(tmp_path)
    asyncio.run(phone.type("Settings"))
    assert phone.device.sent == ["type Settings"]
    with pytest.raises(DeviceError):
        asyncio.run(phone.type("設定"))  # neither ASCII nor Hangul
    with pytest.raises(DeviceError, match="separately"):
        asyncio.run(phone.type("설정 Wi-Fi"))


def test_actor_prompt_explains_input_language_and_fast_clearing():
    from hidagent.agent import ACTOR_PROMPT
    assert "English or Korean" in ACTOR_PROMPT and "key ctrl+space" in ACTOR_PROMPT
    assert "cmd+a then key backspace" in ACTOR_PROMPT
    assert "the keyboard is showing" not in ACTOR_PROMPT  # with a hardware keyboard the on-screen one stays hidden
