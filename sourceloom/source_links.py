"""Recover only unambiguous link destinations already recorded by intake."""
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


def safe_absolute_target(value):
    if (not isinstance(value, str) or not value or
            re.search(r'[\s\x00-\x1f\x7f\\]', value)):
        return False
    try:
        target = urlsplit(value)
        if target.scheme == 'mailto':
            return bool(target.path)
        if target.scheme not in {'http', 'https'}:
            return False
        target.port
        return bool(target.hostname and target.username is None and target.password is None)
    except ValueError:
        return False


def normalize_native_web_target(value):
    """Keep the existing native PDF web spelling without guessing a host."""
    match = re.fullmatch(
        r'(https?):(?:/{0,2})(www\.[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:[:/\?#].*)?)',
        value, re.I)
    if match:
        candidate = match[1].lower()+'://'+match[2]
        if safe_absolute_target(candidate):
            return candidate
    return value


def known_link_targets(inventory):
    """One linear scan, with any conflicting/unsafe saved target excluded.

    The recorded target includes the parser's actual document base (for example
    a published documentation path), which may differ from the download URL.
    """
    candidates = {}
    for obj in inventory.get('objects', []):
        original = obj.get('original_target')
        if obj.get('kind') != 'link' or not isinstance(original, str) or not original:
            continue
        candidates.setdefault(original, set()).add(
            obj.get('target') if safe_absolute_target(obj.get('target')) else None)
    return {original: next(iter(targets)) for original, targets in candidates.items()
            if len(targets) == 1 and None not in targets}


def restore_source_links(raw, targets):
    """Change only HTML hrefs, never Markdown, source bytes or link labels."""
    doc = BeautifulSoup(raw, 'html.parser')
    for anchor in doc.select('a[href]'):
        original = anchor['href']
        normalized = normalize_native_web_target(original)
        if normalized != original:
            anchor['href'] = normalized
            original = normalized
        if original.startswith('#') or original.startswith('assets/'):
            continue
        # Do not turn an unsafe explicit scheme into a trusted link, even when
        # an inconsistent imported inventory happens to claim a safe target.
        try:
            scheme = urlsplit(original).scheme
        except ValueError:
            anchor.attrs.pop('href', None)
            continue
        if not scheme and original in targets:
            anchor['href'] = targets[original]
        elif scheme and not safe_absolute_target(original):
            anchor.attrs.pop('href', None)
    return str(doc)
