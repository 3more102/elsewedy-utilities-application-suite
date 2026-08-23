from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOTS = (ROOT / "app", ROOT / "static")
TEXT_SUFFIXES = {".py", ".js", ".css", ".html"}
KNOWN_MOJIBAKE = {
    "â€”": "—",
    "Ã—": "×",
    "â†’": "→",
    "Â·": "·",
}


def test_runtime_sources_do_not_contain_known_utf8_mojibake():
    offenders = []
    for root in RUNTIME_ROOTS:
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8")
            for broken, intended in KNOWN_MOJIBAKE.items():
                if broken in text:
                    offenders.append(
                        f"{path.relative_to(ROOT)} contains {broken!r}; use {intended!r}"
                    )

    assert not offenders, "Known UTF-8 mojibake found:\n" + "\n".join(offenders)
