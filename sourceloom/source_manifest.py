"""Bind frozen offline assets using explicit, byte-verified source metadata.

This module does not fetch URLs or infer aliases from filenames/content hashes.
"""
import hashlib
import json
import re
from urllib.parse import urlsplit


MANIFEST_NAME = 'SOURCE_MANIFEST.json'


def _path(value):
    if (not isinstance(value, str) or not value or len(value) > 512 or
            '\\' in value or ':' in value or value.startswith('/') or
            re.search(r'[\x00-\x1f\x7f]', value) or
            any(part in {'', '.', '..'} or part.endswith((' ', '.')) or
                re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)
                for part in value.split('/'))):
        raise ValueError('来源清单包含不安全的包内路径')
    return value


def _url(value):
    if (not isinstance(value, str) or not value or len(value) > 8192 or
            re.search(r'[\s\x00-\x1f\x7f\\]', value)):
        raise ValueError('来源清单包含无效 URL')
    try:
        parsed = urlsplit(value)
        parsed.port  # Validate malformed/out-of-range ports without normalizing the URL.
        valid = (parsed.scheme in {'http', 'https'} and parsed.hostname and
                 parsed.username is None and parsed.password is None and
                 not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('来源清单 URL 必须是不含凭据或片段的 HTTP(S) 地址')
    return value


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('来源清单存在重复字段')
        result[key] = value
    return result


def _entry(value, files):
    if not isinstance(value, dict) or set(value) != {'path', 'sha256', 'url'}:
        raise ValueError('来源清单条目需要 path、sha256 和 url')
    path, url = _path(value['path']), _url(value['url'])
    sha = value['sha256']
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{64}', sha):
        raise ValueError('来源清单 SHA-256 无效')
    if path == MANIFEST_NAME or path not in files:
        raise ValueError('来源清单引用的文件不存在')
    if hashlib.sha256(files[path]).hexdigest() != sha:
        raise ValueError('来源清单文件 SHA-256 不匹配')
    return path, url


def resolve_manifest(files, source_url=None, asset_aliases=None):
    """Return existing intake arguments plus a verified provenance receipt.

    Files are the already safely expanded upload. Keep them unchanged; the
    caller archives manifest bytes but excludes this metadata from prose IDs.
    """
    names = [name for name in files
             if name.rsplit('/', 1)[-1].casefold() == MANIFEST_NAME.casefold()]
    if not names:
        return source_url, asset_aliases, None
    if names != [MANIFEST_NAME]:
        raise ValueError('来源清单必须唯一并位于包根目录，名称为 SOURCE_MANIFEST.json')
    raw = files[MANIFEST_NAME]
    if len(raw) > 1024 * 1024:
        raise ValueError('来源清单最多 1 MB')
    try:
        manifest = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_fields)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('来源清单必须是有效 UTF-8 JSON') from exc
    if (not isinstance(manifest, dict) or
            set(manifest) != {'schema', 'source', 'assets'} or
            manifest['schema'] != 'sourceloom-source-manifest/1' or
            not isinstance(manifest['assets'], list) or len(manifest['assets']) > 1000):
        raise ValueError('来源清单格式或版本无效')
    source_path, declared_url = _entry(manifest['source'], files)
    if source_url is not None and source_url != declared_url:
        raise ValueError('来源清单与接入地址冲突')
    aliases = dict(asset_aliases or {})
    seen = set()
    for entry in manifest['assets']:
        path, url = _entry(entry, files)
        if path == source_path or url == declared_url or url in seen:
            raise ValueError('来源清单存在重复或冲突的资源 URL')
        if url in aliases and aliases[url] != path:
            raise ValueError('来源清单与已接入的资源别名冲突')
        aliases[url] = path
        seen.add(url)
    receipt = dict(schema=manifest['schema'], sha256=hashlib.sha256(raw).hexdigest(),
                   source=manifest['source'], assets=manifest['assets'],
                   verification='uploaded_bytes_sha256', external_reads=0)
    return declared_url, aliases, receipt
