from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

_BIDI_NAMES = {
    "\u061c": "ALM",
    "\u200e": "LRM",
    "\u200f": "RLM",
    "\u202a": "LRE",
    "\u202b": "RLE",
    "\u202c": "PDF",
    "\u202d": "LRO",
    "\u202e": "RLO",
    "\u2066": "LRI",
    "\u2067": "RLI",
    "\u2068": "FSI",
    "\u2069": "PDI",
}
_TRUNCATION_MARKER = "<TRUNCATED_FOR_TERMINAL>"


@dataclass(frozen=True, slots=True)
class TerminalSafeText:
    text: str
    encoded_control_count: int
    truncated: bool


def terminal_safe_text(
    value: str,
    *,
    max_output_chars: int = 20_000,
) -> TerminalSafeText:
    if max_output_chars < 0:
        raise ValueError("max_output_chars must be non-negative")

    output: list[str] = []
    output_length = 0
    encoded_controls = 0

    for character in value:
        codepoint = ord(character)
        if character == "\n":
            replacement = "\n"
        elif character == "\t":
            replacement = "    "
            encoded_controls += 1
        elif character in _BIDI_NAMES:
            replacement = f"<{_BIDI_NAMES[character]}>"
            encoded_controls += 1
        elif codepoint == 0x1B:
            replacement = "<ESC>"
            encoded_controls += 1
        elif codepoint == 0x7F:
            replacement = "<DEL>"
            encoded_controls += 1
        elif codepoint == 0x2028:
            replacement = "<LS>"
            encoded_controls += 1
        elif codepoint == 0x2029:
            replacement = "<PS>"
            encoded_controls += 1
        elif codepoint < 0x20 or 0x80 <= codepoint <= 0x9F or 0xD800 <= codepoint <= 0xDFFF:
            replacement = f"<U+{codepoint:04X}>"
            encoded_controls += 1
        else:
            replacement = character

        if output_length + len(replacement) > max_output_chars:
            remaining = max_output_chars - output_length
            if remaining:
                output.append(_TRUNCATION_MARKER[:remaining])
            return TerminalSafeText(
                text="".join(output),
                encoded_control_count=encoded_controls,
                truncated=True,
            )
        output.append(replacement)
        output_length += len(replacement)

    return TerminalSafeText(
        text="".join(output),
        encoded_control_count=encoded_controls,
        truncated=False,
    )


def literal_dynamic_text(value: object, *, max_output_chars: int = 20_000) -> Text:
    encoded = terminal_safe_text(str(value), max_output_chars=max_output_chars)
    return Text(encoded.text)
