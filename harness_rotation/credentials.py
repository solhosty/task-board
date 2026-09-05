"""Resolve Coder tokens from environment overrides or macOS Keychain."""

import os
import shutil
import subprocess
from typing import Any, Dict, Optional


KEYCHAIN_SERVICE = "Harness Rotation Coder"


def keychain_available() -> bool:
    return bool(shutil.which('security'))


def keychain_account(server_id: int) -> str:
    return f'coder-server-{server_id}'


def save_coder_token(server_id: int, token: str) -> None:
    if not keychain_available():
        raise ValueError('macOS Keychain is unavailable; Coder tokens cannot be stored by this Harness server.')
    result = subprocess.run(['security', 'add-generic-password', '-U', '-s', KEYCHAIN_SERVICE,
                             '-a', keychain_account(server_id), '-w', token], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError(result.stderr.strip() or 'Could not save the Coder token in macOS Keychain.')


def remove_coder_token(server_id: int) -> None:
    if not keychain_available():
        return
    subprocess.run(['security', 'delete-generic-password', '-s', KEYCHAIN_SERVICE,
                    '-a', keychain_account(server_id)], capture_output=True, text=True, timeout=10)


def read_coder_token(server: Dict[str, Any]) -> Optional[str]:
    scoped = os.environ.get(f'HARNESS_CODER_TOKEN_{server["id"]}')
    if scoped:
        return scoped
    if os.environ.get('HARNESS_CODER_TOKEN'):
        return os.environ['HARNESS_CODER_TOKEN']
    if not server.get('token_configured') or not keychain_available():
        return None
    result = subprocess.run(['security', 'find-generic-password', '-w', '-s', KEYCHAIN_SERVICE,
                             '-a', keychain_account(server['id'])], capture_output=True, text=True, timeout=10)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None
