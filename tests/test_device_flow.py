import unittest
from unittest.mock import patch
import app


class DeviceFlowTests(unittest.TestCase):
    def test_pending_slowdown_then_completion(self):
        flow = {'expires_at': 999, 'status': 'pending'}
        with patch.object(app.time, 'time', return_value=1), patch.object(app.time, 'sleep') as sleep, patch.object(app, 'device_exchange', side_effect=['authorization_pending', 'slow_down', 'complete']):
            app.finish_device_flow({}, 'secret', 'github', 'private-code', flow, 5)
        self.assertEqual(flow['status'], 'complete')
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [5, 5, 10])

    def test_expired_and_denied(self):
        for result in ['expired_token', 'access_denied']:
            flow = {'expires_at': 999, 'status': 'pending'}
            with patch.object(app.time, 'time', return_value=1), patch.object(app.time, 'sleep'), patch.object(app, 'device_exchange', return_value=result):
                app.finish_device_flow({}, 'secret', 'github', 'private-code', flow, 5)
            self.assertEqual(flow['status'], 'failed')

    def test_start_reuses_pending_and_never_exposes_device_secret(self):
        app.AUTH_FLOWS.clear()
        device = {'device_code':'private-code', 'user_code':'public-code', 'verification_uri':'https://github.com/login/device', 'expires_in':900}
        with patch.object(app, 'read_coder_token', return_value='secret'), patch.object(app, 'coder_json', return_value=device) as get, patch.object(app.threading, 'Thread'):
            first = app.start_device_flow({'id':99,'base_url':'http://localhost:3000'}, 'github')
            second = app.start_device_flow({'id':99,'base_url':'http://localhost:3000'}, 'github')
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)
        self.assertNotIn('device_code', first)
        self.assertNotIn('secret', str(first))
        app.AUTH_FLOWS.clear()
