from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Byte sequences produced when UTF-8 punctuation (em dash, middle dot) is
# misdecoded as cp1252 and re-encoded. Their presence means an editor or
# pipeline step corrupted user-visible operational text.
MOJIBAKE_SEQUENCES = [
    b'\xc3\xa2\xe2\x82\xac\xe2\x80\x9d',  # â€” (broken em dash)
    b'\xc3\xa2\xe2\x82\xac\xe2\x80\x9c',  # â€“ (broken en dash)
    b'\xc3\xa2\xe2\x82\xac\xe2\x80\x99',  # â€™ (broken right single quote)
    b'\xc3\x82\xc2\xb7',  # Â· (broken middle dot)
    b'\xc3\x82\xc2\xa0',  # Â  (broken non-breaking space)
]

SCANNED_PATHS = ['app', 'static', 'scripts']


def test_source_tree_contains_no_double_encoded_punctuation():
    offenders = []
    for folder in SCANNED_PATHS:
        for path in (ROOT / folder).rglob('*'):
            if path.suffix not in ('.py', '.js', '.html', '.css'):
                continue
            if '__pycache__' in path.parts:
                continue
            data = path.read_bytes()
            for sequence in MOJIBAKE_SEQUENCES:
                if sequence in data:
                    offenders.append(f'{path}: {sequence!r}')
    assert offenders == [], 'double-encoded text found in: ' + ', '.join(offenders)


def test_operational_strings_keep_legitimate_utf8_punctuation():
    # The guard must not ban legitimate UTF-8 punctuation: mainline
    # application text intentionally uses real em dashes.
    application = (ROOT / 'app' / 'application.py').read_bytes()
    assert application.count(b'\xe2\x80\x94') >= 1
