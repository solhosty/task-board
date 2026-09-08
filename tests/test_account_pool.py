import unittest
from datetime import datetime, timedelta, timezone

from harness_rotation.account_pool import next_binding


class AccountPoolTests(unittest.TestCase):
    def test_uses_second_codex_before_claude(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        bindings = [
            {'id': 1, 'provider': 'codex', 'label': 'codex-1', 'priority': 0, 'enabled': 1, 'cooldown_until': future},
            {'id': 2, 'provider': 'codex', 'label': 'codex-2', 'priority': 1, 'enabled': 1},
            {'id': 3, 'provider': 'claude', 'label': 'claude-1', 'priority': 0, 'enabled': 1},
        ]
        self.assertEqual(next_binding(bindings, ('codex', 'claude'))['label'], 'codex-2')

    def test_falls_through_provider_order(self):
        bindings = [{'id': 1, 'provider': 'claude', 'label': 'claude-1', 'priority': 0, 'enabled': 1}]
        self.assertEqual(next_binding(bindings, ('codex', 'claude'))['label'], 'claude-1')
