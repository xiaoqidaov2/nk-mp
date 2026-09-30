"""Aliyun ``acw_sc__v2`` WAF challenge solver for the Nike identity host.

``accounts.nike.com.cn`` protects the SMS endpoints with the classic acw JS
challenge: the edge answers with an HTML page carrying ``arg1`` and the client
is expected to compute ``acw_sc__v2 = hexXor(unsbox(arg1), KEY)`` and retry
with that cookie. The algorithm below was recovered from the publicly served
challenge script (see the frozen contract record) and verified end to end on
2026-09-29 before this module was written.
"""

from __future__ import annotations

import re

ACW_KEY = "3000176000856006061501533003690027800375"

# 40-entry permutation taken verbatim from the served challenge script.
ACW_TABLE = (
    0xF, 0x23, 0x1D, 0x18, 0x21, 0x10, 0x1, 0x26, 0xA, 0x9, 0x13, 0x1F, 0x28,
    0x1B, 0x16, 0x17, 0x19, 0xD, 0x6, 0xB, 0x27, 0x12, 0x14, 0x8, 0xE, 0x15,
    0x20, 0x1A, 0x2, 0x1E, 0x7, 0x4, 0x11, 0x5, 0x3, 0x1C, 0x22, 0x25, 0xC,
    0x24,
)

_ARG1_RE = re.compile(rb"arg1='([0-9A-Fa-f]+)'")


def unsbox(value: str) -> str:
    """Reorder the 40 characters of ``arg1`` by the recovered table."""
    out = [""] * len(ACW_TABLE)
    for index, char in enumerate(value):
        for slot, position in enumerate(ACW_TABLE):
            if position == index + 1:
                out[slot] = char
    return "".join(out)


def hex_xor(value: str, key: str) -> str:
    """Byte-wise (two hex digits) XOR of ``value`` with ``key``."""
    result = ""
    for index in range(0, min(len(value), len(key)) - 1, 2):
        left = int(value[index : index + 2], 16)
        right = int(key[index : index + 2], 16)
        result += format(left ^ right, "x").rjust(2, "0")
    return result


def solve_acw_sc_v2(arg1: str) -> str:
    return hex_xor(unsbox(arg1), ACW_KEY)


def extract_arg1(body: bytes) -> str | None:
    match = _ARG1_RE.search(body)
    return match.group(1).decode() if match else None


def looks_like_challenge(body: bytes) -> bool:
    return bool(ACW_KEY and extract_arg1(body) and b"acw_sc__v2" in body)
