"""Engine-aware checks shared by translation and saved review maintenance."""

import re

from etutane_tools.libraries.furigana import without_furigana


_ENGINE_TOKEN = re.compile(r"(?:\\x[0-9A-Fa-f]{2}|«[0-9A-Fa-f]{2}»)")


def engine_tokens(text, schema=None):
    if schema == "etutane":
        text = without_furigana(text)
    return _ENGINE_TOKEN.findall(text)
