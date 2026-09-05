"""Coder server naming, URL validation, and public presentation."""

import hashlib
import json
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse


def slug(value: str, fallback: str) -> str:
    result = re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')
    return (result or fallback)[:48]


def normalize_coder_url(value: Any) -> str:
    parsed = urlparse(str(value or '').strip())
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Enter a Coder server URL such as http://127.0.0.1:3000.')
    if parsed.path not in ('', '/'):
        raise ValueError('Use the Coder server base URL without a path.')
    return f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')


def coder_template_name(server_name: str, project_name: str) -> str:
    candidate = f'harness-{slug(server_name, "server")}-{slug(project_name, "project")}'
    if len(candidate) <= 32:
        return candidate
    suffix = hashlib.sha1(candidate.encode('utf-8')).hexdigest()[:6]
    return f'{candidate[:25].rstrip("-")}-{suffix}'


def public_coder_server(server: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(server)
    try:
        result['capabilities'] = json.loads(result.pop('capabilities_json') or '{}')
    except json.JSONDecodeError:
        result['capabilities'] = {}
    result['token_configured'] = bool(result['token_configured'])
    return result


def safe_install_url(value: Any) -> Optional[str]:
    parsed = urlparse(str(value or ''))
    return str(value) if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password else None
