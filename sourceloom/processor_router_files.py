"""Verify the existing Router's inline Codex file result, without editing it."""

import base64
import binascii
import hashlib
import re


MAX_OUTPUT_FILE_BYTES = 1024 * 1024
_WINDOWS_DEVICES = {'CON', 'PRN', 'AUX', 'NUL'} | {
    prefix + str(number) for prefix in ('COM', 'LPT') for number in range(1, 10)}


def output_path(value):
    """Match the Router's bounded relative file contract; never resolve locally."""
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError('Router 输出文件路径必须是非空的相对路径，最多 512 字符')
    if any(ord(char) < 32 or ord(char) == 127 or char in '\\<>:"|?*' for char in value):
        raise ValueError('Router 输出文件路径含有不允许的字符')
    parts = value.split('/')
    if any(part in {'', '.', '..'} or part.endswith(('.', ' ')) or
           part.split('.')[0].upper() in _WINDOWS_DEVICES for part in parts):
        raise ValueError('Router 输出文件路径不能是绝对路径、父目录或保留文件名')
    return value


def read_markdown_file(output, expected_path):
    """Return original bytes plus exact UTF-8 Markdown from one required file."""
    expected_path = output_path(expected_path)
    files = output.get('files') if isinstance(output, dict) else None
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise ValueError('Router 完整任务未返回唯一的预期 Markdown 文件')
    file = files[0]
    if output_path(file.get('path')) != expected_path:
        raise ValueError('Router 返回文件路径与原请求的预期 Markdown 文件不一致')
    size = file.get('sizeBytes')
    if type(size) is not int or not 0 < size <= MAX_OUTPUT_FILE_BYTES:
        raise ValueError('Router Markdown 文件大小无效或超过 1 MiB，未截断正文')
    checksum = file.get('sha256')
    if not isinstance(checksum, str) or not re.fullmatch('[0-9a-f]{64}', checksum):
        raise ValueError('Router Markdown 文件缺少有效的 SHA-256')
    content = file.get('content')
    if not isinstance(content, str):
        raise ValueError('Router Markdown 文件没有可验证的内容')
    encoding = file.get('encoding')
    if (encoding == 'utf8' and len(content) > MAX_OUTPUT_FILE_BYTES or
            encoding == 'base64' and len(content) > 4 * ((MAX_OUTPUT_FILE_BYTES + 2) // 3)):
        raise ValueError('Router Markdown 文件内容超过 1 MiB，未截断正文')
    try:
        if encoding == 'utf8':
            raw = content.encode('utf-8')
        elif encoding == 'base64':
            raw = base64.b64decode(content, validate=True)
        else:
            raise ValueError('Router Markdown 文件编码不是 utf8 或 base64')
    except (UnicodeEncodeError, binascii.Error) as error:
        raise ValueError('Router Markdown 文件编码内容无效') from error
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != checksum:
        raise ValueError('Router Markdown 文件字节数或 SHA-256 不一致，未接受正文')
    try:
        markdown = raw.decode('utf-8')
    except UnicodeDecodeError as error:
        raise ValueError('Router Markdown 文件不是有效 UTF-8') from error
    if not markdown.removeprefix('\ufeff').strip():
        raise ValueError('Router Markdown 文件没有可用正文')
    return raw, markdown, dict(path=expected_path, encoding=encoding,
                              size_bytes=size, sha256=checksum)
