"""Disposable visual fixture; never launches a harness or uses the real database."""
import functools
import json
import sys
import tempfile
import threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app

with tempfile.TemporaryDirectory(prefix='rotation-chat-preview-') as directory:
    root=Path(directory)
    app.DATA_ROOT=root/'state'
    app.DB_PATH=app.DATA_ROOT/'state.sqlite3'
    app.init_db()
    app.execute('UPDATE harnesses SET installed=1,enabled=1')
    for adapter in app.ADAPTERS.values():
        adapter['build']=lambda *args:[sys.executable,'-c','print("Preview worker only")']
    project=app.execute('INSERT INTO projects(name,repo_path,verify_command,created_at) VALUES(?,?,?,?)',('Chat rendering fixture',str(root),'true',app.now()))
    task=app.execute('INSERT INTO tasks(project_id,text,task_order,created_at) VALUES(?,?,0,?)',(project,'One conversation across four harnesses',app.now()))
    app.execute("INSERT INTO task_messages(task_id,role,content,created_at) VALUES(?,'user',?,?)",(task,'Build the feature and preserve the existing code.',app.now()))
    for key in ('codex','droid','opencode','claude'):
        attempt=app.execute("INSERT INTO attempts(task_id,harness_key,model,status,started_at,worktree_path,run_id) VALUES(?,?,'default','completed',?,?,'fixture')",(task,key,app.now(),str(root)))
        content='## '+app.ADAPTERS[key]['label']+' progress\n\nImplemented **this part** and ran `tests`.\n\n- Preserved existing changes\n- Ready for the next step\n\n```rust\n'+('\n'.join(f'pub struct Example{i} {{ value: String }}' for i in range(35)))+'\n```'
        app.execute("INSERT INTO task_messages(task_id,role,content,created_at,attempt_id) VALUES(?,'assistant',?,?,?)",(task,content,app.now(),attempt))
        history=root/f'{key}.log'
        history.write_text('\n'.join(map(json.dumps,[{'type':'item.completed','item':{'id':'note1','type':'agent_message','text':'I will inspect the implementation before making changes.'}},{'type':'item.completed','item':{'id':'cmd1','type':'command_execution','command':'node --test','status':'completed','aggregated_output':'All fixture tests passed.'}},{'type':'item.completed','item':{'id':'note2','type':'agent_message','text':'The existing behavior is covered. I can now make the focused change.'}}])))
        app.execute('UPDATE attempts SET log_path=? WHERE id=?',(str(history),attempt))
    log=root/'fixture.log'
    log.write_text(json.dumps({'type':'result','result':'The edit is ready, but I need permission to run the build before I can verify it.','permission_denials':[{'tool_name':'Bash','tool_input':{'command':'cargo build'}}]}))
    app.execute("UPDATE attempts SET status='failed',log_path=? WHERE id=?",(str(log),attempt))
    app.execute("INSERT INTO runs(id,project_id,task_id,mode,status,message,attempt_id,created_at,updated_at) VALUES('fixture',?,?,'supervised','stopped','The harness needs permission.',?,?,?)",(project,task,attempt,app.now(),app.now()))
    server=app.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(app.API,directory=str(app.STATIC_ROOT)))
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    print(f'CHAT_PREVIEW http://127.0.0.1:{server.server_port}',flush=True)
    try:
        threading.Event().wait(180)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown();server.server_close();thread.join()
print('Chat preview stopped; disposable state removed.',flush=True)
