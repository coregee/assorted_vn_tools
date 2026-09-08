"""Recognize the furigana form observed in Etsuraku no Tane script text.

Keep the raw extraction reversible. Only complete FF FF 01 reading 02 spans
are annotations; isolated bytes and damaged spans must remain visible to checks.
"""

import re


FURIGANA = re.compile(r"«FF»«FF»«01»([^«»\r\n]+)«02»", re.IGNORECASE)


def without_furigana(text):
    """Remove complete reading annotations, leaving the following base text."""
    return FURIGANA.sub("", text)


def furigana_for_prompt(text):
    """Expose readings as context without asking a model to copy binary markup."""
    return FURIGANA.sub(lambda match: "[furigana reading: %s]" % match.group(1), text)
