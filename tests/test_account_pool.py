import unittest
from datetime import datetime, timedelta, timezone

from harness_rotation.account_pool import next_binding


class AccountPoolTests(unittest.TestCase):
    def test_full_account_chain_and_reset_without_model_calls(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        bindings = [
            {'id': 1, 'provider': 'codex', 'label': 'hunter', 'priority': 1, 'enabled': 1},
            {'id': 2, 'provider': 'codex', 'label': 'hostynft', 'priority': 0, 'enabled': 1},
            {'id': 3, 'provider': 'claude', 'label': 'claude', 'priority': 2, 'enabled': 1},
        ]
        self.assertEqual(next_binding(bindings, ('codex', 'claude'))['label'], 'hostynft')
        bindings[1]['cooldown_until'] = future
        self.assertEqual(next_binding(bindings, ('codex', 'claude'))['label'], 'hunter')
        bindings[0]['cooldown_until'] = future
        self.assertEqual(next_binding(bindings, ('codex', 'claude'))['label'], 'claude')
        bindings[2]['cooldown_until'] = future
        self.assertIsNone(next_binding(bindings, ('codex', 'claude')))
        bindings[1]['cooldown_until'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.assertEqual(next_binding(bindings, ('codex', 'claude'))['label'], 'hostynft')

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
