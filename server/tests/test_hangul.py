"""Korean text is typed through the board as the keys of the Korean 2-set (dubeolsik) layout (#17-E).

With the board recognized as a hardware keyboard, iOS reads its keys with the current input language; in Korean
the 2-set layout composes syllables from these keys exactly like a Korean keyboard.
"""

import asyncio

import pytest

from hidagent.device import DeviceError
from hidagent.hangul import compose_dubeolsik, has_hangul, to_dubeolsik


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


@pytest.mark.parametrize("text, keys", [
    ("설정", "tjfwjd"), ("갈비", "rkfql"), ("괜찮아", "rhoscksgdk"), ("테스트플라이트", "xptmxmvmffkdlxm"),
    ("까치 꽃 쌀", "Rkcl Rhc Tkf"), ("얘기 예", "dOrl dP"), ("값", "rkqt"), ("카톡 123!", "zkxhr 123!"),
    ("ㅋㅋ ㅠ", "zz b"),
])
def test_dubeolsik_keys_compose_back_into_the_text(text, keys):
    assert compose_dubeolsik(keys) == text  # the IME's composition, the reverse of to_dubeolsik


@pytest.mark.parametrize("keys, shown", [
    ("Safari", "ㄴㅁㄹㅁ갸"),   # device 2026-10-22: English typed with Korean active (S -> ㄴ, no double for ㄴ)
    ("tlrP", "시계"),           # the keys of 시계: a final ㄱ becomes the next initial when ㅖ follows
    ("weather", "ㅈㄷㅁ솓ㄱ"),  # consonants without a vowel stay apart; ㄷ ends 소; ㄷㄱ is no compound final
    ("Clock", "치ㅐ차"),        # a second vowel that makes no compound starts over as a bare vowel
    ("rkqtl", "갑시"),          # a compound final ㅄ splits: ㅂ stays, ㅅ carries into 시
    ("go 7", "해 7"),           # digits and spaces pass through
])
def test_english_keys_show_as_the_imes_hangul(keys, shown):
    assert compose_dubeolsik(keys) == shown


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


def test_script_runs_split_mixed_text_keeping_neutral_characters_with_their_run():
    from hidagent.hangul import script_runs
    assert script_runs("설정") == [(True, "설정")]
    assert script_runs("Wi-Fi 12") == [(False, "Wi-Fi 12")]
    assert script_runs("교보eBook") == [(True, "교보"), (False, "eBook")]
    assert script_runs("설정 Wi-Fi 켜기") == [(True, "설정 "), (False, "Wi-Fi "), (True, "켜기")]
    assert script_runs("123 abc") == [(False, "123 abc")] and script_runs("123 가") == [(True, "123 가")]
    assert script_runs("") == []


def test_phone_types_mixed_text_run_by_run_switching_the_input_language(tmp_path):
    # a Korean run, switch, an English run, and the switch back so the phone keeps the language it started in
    phone = make_phone(tmp_path)
    asyncio.run(phone.type("교보eBook"))
    assert phone.device.sent == ["type ryqh", "key ctrl+space", "type eBook", "key ctrl+space"]
    phone = make_phone(tmp_path)
    asyncio.run(phone.type("설정 Wi-Fi 켜기"))  # three runs: two switches, already back in Korean
    assert phone.device.sent == ["type tjfwjd ", "key ctrl+space", "type Wi-Fi ", "key ctrl+space", "type zurl"]


def test_actor_prompt_explains_input_language_and_fast_clearing():
    from hidagent.agent import ACTOR_PROMPT
    assert "English, Korean or both" in ACTOR_PROMPT and "key ctrl+space" in ACTOR_PROMPT
    assert "cmd+a then key backspace" in ACTOR_PROMPT
    assert "the keyboard is showing" not in ACTOR_PROMPT  # with a hardware keyboard the on-screen one stays hidden


def test_absolute_pointer_parks_after_a_tap_and_a_drag_not_after_typing(tmp_path):
    from hidagent.phone import ABS_MAX, PARK
    phone = make_phone(tmp_path)
    asyncio.run(phone.tap(0.5, 0.5))
    park = f"at {round(PARK[0] * ABS_MAX)} {round(PARK[1] * ABS_MAX)}"
    assert phone.device.sent[-2:] == ["click", park]
    phone.device.sent.clear()
    asyncio.run(phone.swipe(0.5, 0.8, 0.5, 0.5))
    assert phone.device.sent[-2:] == ["release", park]
    phone.device.sent.clear()
    asyncio.run(phone.type("abc"))
    assert phone.device.sent == ["type abc"]
