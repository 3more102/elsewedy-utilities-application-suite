import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / 'static'
APP_JS = STATIC / 'app.js'
INDEX = STATIC / 'index.html'

STYLE_ATTRIBUTE = re.compile(r'\bstyle\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
DIRECT_STYLE_API = re.compile(
    r'(?:\.style(?:\.|\[)|\.cssText\b|setAttribute\s*\(\s*["\']style["\'])',
    re.IGNORECASE,
)

# Transitional debt inherited from the monolithic renderer. Static values should
# move to CSS classes; the dynamic values require dedicated render refactors
# before production can safely drop style-src 'unsafe-inline'.
ALLOWED_STATIC_STYLES = {
    'margin-top:14px',
    'margin-top:12px',
    'margin-top:15px',
    'grid-template-columns:1fr 1fr',
    'display:flex;gap:8px;align-items:center;font-size:11px;margin-top:10px',
}


def _allowed_app_style(value: str) -> bool:
    if value in ALLOWED_STATIC_STYLES:
        return True
    if re.fullmatch(r'width:\$\{.+\}%', value):
        return True
    if value.startswith('background:conic-gradient(') and value.endswith(')'):
        return True
    if re.fullmatch(r'left:\$\{p\.x\}%;top:\$\{p\.y\}%', value):
        return True
    return False


def test_inline_style_debt_is_confined_to_known_app_renderer_patterns():
    assert not STYLE_ATTRIBUTE.findall(INDEX.read_text(encoding='utf-8'))

    total = 0
    unexpected: list[tuple[str, str]] = []
    for path in sorted(STATIC.glob('*.js')):
        source = path.read_text(encoding='utf-8')
        styles = STYLE_ATTRIBUTE.findall(source)
        if path != APP_JS:
            unexpected.extend((path.name, value) for value in styles)
            continue
        total = len(styles)
        unexpected.extend((path.name, value) for value in styles if not _allowed_app_style(value))

    assert not unexpected, unexpected
    # Keep a hard ceiling so even an allowed legacy value cannot proliferate
    # silently. The target is zero; this gate only prevents debt growth while
    # the dynamic chart/GIS/progress renderers are decomposed.
    assert total <= 25, f'inline style debt grew to {total} app.js attributes'


def test_frontend_does_not_add_direct_dom_style_mutation_apis():
    offenders: list[str] = []
    for path in sorted(STATIC.glob('*.js')):
        if DIRECT_STYLE_API.search(path.read_text(encoding='utf-8')):
            offenders.append(path.name)
    assert offenders == [], offenders
