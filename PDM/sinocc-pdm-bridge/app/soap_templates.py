"""SOAP oStr templates for the SinoCC PDM client protocol.

Populated from the Fiddler capture (docs/captured/). Each entry maps a
``(formname, method)`` to a function that renders the CDATA ``oStr`` XML from a
dict of fields. `render()` is called by `client_proto.SinoCCSoapClient.call_template`.

Until the capture is parsed (task #14), this registry is empty — `render()` raises
a clear error naming what's missing. Fill entries by copying the exact oStr bytes
from the parsed capture (docs/captured/parsed_<file>.json), preserving every
attribute/element order Resin expects.
"""
from __future__ import annotations

from typing import Callable, Dict

# (formname, method) -> builder(fields: dict) -> oStr xml string
TEMPLATES: Dict[tuple, Callable[[dict], str]] = {}

# Raw oStr samples captured verbatim, for reference / exact replay.
# keyed by (formname, method); value is the literal CDATA content.
RAW_SAMPLES: Dict[tuple, str] = {}


def register(formname: str, method: str, builder: Callable[[dict], str]) -> None:
    TEMPLATES[(formname, method)] = builder


def register_raw(formname: str, method: str, ostr_xml: str) -> None:
    RAW_SAMPLES[(formname, method)] = ostr_xml


def render(formname: str, method: str, fields: dict) -> str:
    key = (formname, method)
    if key in RAW_SAMPLES and not fields:
        return RAW_SAMPLES[key]
    if key in TEMPLATES:
        return TEMPLATES[key](fields)
    avail = ", ".join(f"{f}.{m}" for f, m in sorted(set(TEMPLATES) | set(RAW_SAMPLES)))
    raise NotImplementedError(
        f"No SOAP template for {formname}.{method}. Available: {avail or '(none yet)'}. "
        f"Parse the Fiddler capture first (docs/captured/parse_fiddler.py)."
    )


__all__ = ["TEMPLATES", "RAW_SAMPLES", "register", "register_raw", "render"]
