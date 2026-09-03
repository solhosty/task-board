import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from events import read_events

class ChatOutputTests(unittest.TestCase):
    def test_codex_reply_excludes_commands(self):
        events = [
            {'type':'item.completed','item':{'type':'command_execution','aggregated_output':'huge source dump'}},
            {'type':'item.completed','item':{'type':'agent_message','text':'## Done\nThe change is ready.'}},
        ]
        reply, error = app.decode_result('codex', '\n'.join(map(json.dumps,events)))
        self.assertEqual(reply, '## Done\nThe change is ready.')
        self.assertIsNone(error)

    def test_bounded_activity_and_saved_session(self):
        with tempfile.TemporaryDirectory(prefix='rotation-chat-test-') as directory:
            file = Path(directory)/'output.log'
            session = '01a068f6-cda1-7fd2-9e96-91e744882f8c'
            events = [json.dumps({'type':'thread.started','thread_id':session})]
            events += [json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'cat src/main.rs','aggregated_output':'x'*4000,'status':'completed'}})]*20
            file.write_text('\n'.join(events))
            result = app.log_details({'log_path':str(file),'harness_key':'codex'})
            self.assertEqual(result['resume_command'], 'codex resume '+session)
            self.assertLessEqual(len(result['log']),30000)
            self.assertLessEqual(len(result['activity']),6)
            self.assertTrue(all(a['text']=='cat src/main.rs' for a in result['activity']))

    def test_legacy_session_header(self):
        with tempfile.TemporaryDirectory(prefix='rotation-chat-test-') as directory:
            file = Path(directory)/'output.log'
            file.write_text('session id: 01a068f6-cda1-7fd2-9e96-91e744882f8c\nold text output')
            result=app.log_details({'log_path':str(file),'harness_key':'codex'})
            self.assertTrue(result['resume_command'])
            self.assertEqual(result['activity'],[])

    def test_shared_activity_format_for_each_harness(self):
        events = {
            'codex': {'type':'item.completed','item':{'type':'agent_message','text':'Progress'}},
            'claude': {'type':'assistant','message':{'content':[{'type':'text','text':'Progress'}]}},
            'droid': {'type':'message','role':'assistant','text':'Progress'},
            'opencode': {'type':'text','part':{'text':'Progress'}},
        }
        with tempfile.TemporaryDirectory(prefix='rotation-chat-test-') as directory:
            file=Path(directory)/'output.log'
            for key,event in events.items():
                file.write_text(json.dumps(event))
                self.assertEqual(app.log_details({'log_path':str(file),'harness_key':key})['activity'],[{'kind':'message','text':'Progress','status':''}])

    def test_command_start_and_result_become_one_persistent_embed(self):
        with tempfile.TemporaryDirectory(prefix='rotation-chat-test-') as directory:
            path=Path(directory)/'events.log'
            records=[{'type':'item.started','item':{'id':'cmd1','type':'command_execution','command':'npm test','status':'in_progress'}},
                     {'type':'item.completed','item':{'id':'cmd1','type':'command_execution','command':'npm test','status':'completed','aggregated_output':'12 tests passed'}}]
            path.write_text('\n'.join(map(json.dumps,records)))
            events=read_events(path)
            self.assertEqual(len(events),1)
            self.assertEqual(events[0]['output'],'12 tests passed')
            self.assertEqual(events[0]['status'],'completed')
            self.assertEqual(read_events(path),events)

    def test_reasoning_summaries_are_saved_separately_from_replies(self):
        with tempfile.TemporaryDirectory(prefix='rotation-chat-test-') as directory:
            path=Path(directory)/'events.log'
            records=[
                {'type':'item.completed','item':{'id':'reason1','type':'reasoning','summary':[{'type':'summary_text','text':'I will inspect the failing test first.'}]}},
                {'type':'assistant','message':{'content':[{'type':'thinking','summary':'The parser is the likely source of the failure.'}]}},
            ]
            path.write_text('\n'.join(map(json.dumps,records)))
            events=read_events(path)
            self.assertEqual([event['kind'] for event in events],['reasoning','reasoning'])
            self.assertIn('failing test',events[0]['text'])
            self.assertIn('parser',events[1]['text'])

    def test_claude_tool_result_keeps_tool_name_and_output(self):
        with tempfile.TemporaryDirectory(prefix='rotation-chat-test-') as directory:
            path=Path(directory)/'events.log'
            records=[{'type':'assistant','message':{'content':[{'type':'tool_use','id':'tool1','name':'Read','input':{'file_path':'src/app.py'}}]}},
                     {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'tool1','content':'file contents'}]}}]
            path.write_text('\n'.join(map(json.dumps,records)))
            events=read_events(path)
            self.assertEqual(len(events),1)
            self.assertIn('Read',events[0]['text'])
            self.assertEqual(events[0]['output'],'file contents')

    def test_permission_failure_retains_natural_language_and_specific_status(self):
        with tempfile.TemporaryDirectory(prefix='rotation-chat-test-') as directory:
            path=Path(directory)/'events.log'
            path.write_text(json.dumps({'type':'result','is_error':False,'result':'I made the edits, but cargo needs permission.','permission_denials':[{'tool_name':'Bash','tool_input':{'command':'cargo build'}}]}))
            attempt=app.present_attempt({'status':'failed','log_path':str(path)})
            self.assertEqual(attempt['display_status'],'Needs permission')
            self.assertIn('Bash',attempt['display_reason'])
            self.assertTrue(any(e['kind']=='message' and 'made the edits' in e['text'] for e in read_events(path)))

    def test_old_codex_transcript_recovers_spoken_updates_not_tool_dump(self):
        with tempfile.TemporaryDirectory(prefix='rotation-chat-test-') as directory:
            path=Path(directory)/'events.log'
            path.write_text('OpenAI Codex v0.149.0\nuser\nDo the work\ncodex\nI will inspect the code.\nexec\ncat main.rs\nHUGE SOURCE DUMP\ncodex\nI found the problem.\nexec\nnode test.js\ntokens used\n1000\n')
            self.assertEqual([e['text'] for e in read_events(path)],['I will inspect the code.','I found the problem.'])

    def test_auto_review_command_does_not_bypass_permissions(self):
        command=app.configured_command({'key':'claude','model':'default','tool_permissions':'auto'},Path('/tmp'),'Test')
        self.assertEqual(command[command.index('--permission-mode')+1],'auto')
        self.assertNotIn('--dangerously-skip-permissions',command)

if __name__=='__main__':
    unittest.main(verbosity=2)
