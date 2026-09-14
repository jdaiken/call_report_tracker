#!/usr/bin/env python3
"""One-off diagnostic: report SAFE metadata about FFIEC_USER_ID/FFIEC_TOKEN --
never the actual value -- to explain why the real secret behaves differently
against FFIEC's gateway than an obviously-fake placeholder string does.
Delete this script once the mismatch is understood; it's not part of the
regular pipeline.
"""
from __future__ import annotations

import os
import unicodedata


def _describe(name: str) -> None:
    raw = os.environ.get(name)
    if raw is None:
        print(f"{name}: NOT SET")
        return

    stripped = raw.strip()
    non_ascii = [c for c in raw if ord(c) > 127]
    control_chars = [c for c in raw if unicodedata.category(c).startswith("C")]
    interior_whitespace = any(c.isspace() for c in raw[1:-1]) if len(raw) > 2 else False

    print(f"{name}:")
    print(f"  raw length: {len(raw)}")
    print(f"  length after .strip(): {len(stripped)}")
    print(f"  first char repr: {raw[0]!r}" if raw else "  (empty)")
    print(f"  last char repr: {raw[-1]!r}" if raw else "")
    print(f"  contains non-ASCII characters: {len(non_ascii)} (codepoints: {[hex(ord(c)) for c in non_ascii[:10]]})")
    print(f"  contains control/formatting characters: {len(control_chars)} "
          f"(categories: {[unicodedata.category(c) for c in control_chars[:10]]})")
    print(f"  contains whitespace NOT at the ends: {interior_whitespace}")
    print(f"  looks like a JWT (two periods, base64url-ish segments): {_looks_like_jwt(stripped)}")


def _looks_like_jwt(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 3:
        return False
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    return all(part and set(part) <= allowed for part in parts)


if __name__ == "__main__":
    _describe("FFIEC_USER_ID")
    print()
    _describe("FFIEC_TOKEN")
