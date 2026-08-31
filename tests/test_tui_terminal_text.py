from __future__ import annotations

from worktrace.tui.terminal_text import literal_dynamic_text, terminal_safe_text


def test_terminal_safe_text_encodes_full_control_and_bidi_corpus() -> None:
    controls = "".join(chr(value) for value in range(0x20) if value not in {0x09, 0x0A})
    c1 = "".join(chr(value) for value in range(0x80, 0xA0))
    bidi = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    source = (
        "ordinary Ω\n\ttab"
        + controls
        + "\x1b[31m"
        + "\x1b]8;;https://example.test\x07link\x1b]8;;\x1b\\"
        + "\x1b]52;c;Y29weQ==\x07"
        + "\x1bPpayload\x1b\\"
        + c1
        + "\x7f\u2028\u2029"
        + bidi
        + "\ud800\udfff"
        + "[bold][@click=app.quit]literal[/]"
    )

    encoded = terminal_safe_text(source, max_output_chars=20_000)

    assert "ordinary Ω\n    tab" in encoded.text
    assert "<ESC>[31m" in encoded.text
    assert "<RLO>" in encoded.text
    assert "<U+D800><U+DFFF>" in encoded.text
    assert "[bold][@click=app.quit]literal[/]" in encoded.text
    assert encoded.encoded_control_count > 70
    assert encoded.truncated is False
    assert all(character == "\n" or ord(character) >= 0x20 for character in encoded.text)
    assert not any(0x7F <= ord(character) <= 0x9F for character in encoded.text)


def test_terminal_safe_text_bounds_replacement_expansion() -> None:
    assert terminal_safe_text("unsafe\x1b", max_output_chars=0).text == ""
    for bound in (1, 3, 5, 10, 24):
        encoded = terminal_safe_text("\x1b" * 100, max_output_chars=bound)
        assert len(encoded.text) <= bound
        assert encoded.truncated is True
        assert "\x1b" not in encoded.text


def test_literal_dynamic_text_has_no_markup_spans_or_links() -> None:
    rendered = literal_dynamic_text("[bold][@click=app.quit]literal[/][/]")

    assert rendered.plain == "[bold][@click=app.quit]literal[/][/]"
    assert rendered.spans == []
