import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Paperclip,
  Send,
  ExternalLink,
  RotateCcw,
  GitPullRequest,
  Terminal,
  ShieldCheck,
  Clock3,
  MessageSquareText,
  AlertTriangle,
  Play,
  X,
  FileText,
  Activity,
  FolderCode,
  FilePenLine,
  Wrench,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import '../live-activity.css';
import { api, type Project, type Task, label, stageOf } from '@/lib/api';
import { attachmentName, readBase64, readableSize, type Attachment } from '@/lib/files';
import { Button } from './ui/button';
import { Modal, Tabs, Textarea, Empty, Field, Select } from './ui/controls';
import { HarnessMark } from './Board';
type Detail = {
  task: Task;
  messages: any[];
  sessions: any[];
  attempts: any[];
  run: any;
  pull_requests: any[];
  blockers: string[];
  lease: any;
  attachments: Attachment[];
  delivery_recovery?: { title: string; detail: string; run_id: string } | null;
  connection_action?: { server_id: number; provider: string } | null;
};
function Attachments({
  items,
  taskId,
  onRemove,
}: {
  items: Attachment[];
  taskId: number;
  onRemove?: (a: Attachment) => void;
}) {
  if (!items.length) return null;
  return (
    <div className="attachments">
      {items.map((a) => (
        <figure className={'attachment ' + a.kind} key={a.id}>
          <a href={`/api/tasks/${taskId}/attachments/${a.id}`} target="_blank" rel="noreferrer">
            {a.kind === 'image' ? (
              <img src={`/api/tasks/${taskId}/attachments/${a.id}`} alt={a.filename} />
            ) : (
              <FileText size={15} />
            )}
            <figcaption>
              {a.filename}
              <small>{readableSize(a.byte_size)}</small>
            </figcaption>
          </a>
          {onRemove && (
            <button aria-label={`Remove ${a.filename}`} onClick={() => onRemove(a)}>
              <X size={13} />
            </button>
          )}
        </figure>
      ))}
    </div>
  );
}
function eventTitle(s: any) {
  return s.close_reason
    ? `Session ${s.session_number} ended · ${s.close_reason}`
    : `Session ${s.session_number} started${s.harness_key ? ` on ${label(s.harness_key)}` : ''}`;
}
const KIND_ICON: Record<string, any> = {
  message: MessageSquareText,
  reasoning: Activity,
  command: Terminal,
  tool: Wrench,
  edit: FilePenLine,
  permission: AlertTriangle,
  error: AlertTriangle,
  result: Wrench,
};
function LiveEvent({ item }: { item: any }) {
  const Icon = KIND_ICON[item.kind] || Activity;
  const busy = ['working', 'in_progress'].includes(item.status);
  return (
    <div className={`live-event live-${item.kind}${item.status === 'failed' ? ' failed' : ''}`}>
      <div className="live-event-head">
        <Icon size={13} />
        <span className="live-event-text">{item.text || label(item.kind)}</span>
        {item.status && <small className={busy ? 'busy' : ''}>{label(item.status)}</small>}
      </div>
      {item.kind === 'permission' && item.requests?.length > 0 && (
        <ul className="live-permissions">
          {item.requests.map((r: any, i: number) => (
            <li key={i}>
              <b>{r.tool}</b>
              {r.command && <code>{r.command}</code>}
            </li>
          ))}
        </ul>
      )}
      {item.output?.trim() && (
        <details className="live-output">
          <summary>Output</summary>
          <pre>{item.output}</pre>
        </details>
      )}
    </div>
  );
}
function LiveActivity({
  attempt,
  active,
  replies,
}: {
  attempt: any;
  active: boolean;
  replies: string[];
}) {
  const enabled = !!attempt;
  const { data: log } = useQuery({
    queryKey: ['attempt-log', attempt?.id],
    queryFn: () => api<{ events: any[] }>(`/api/attempts/${attempt.id}/log`),
    enabled,
    refetchInterval: active ? 1500 : false,
  });
  const { data: changes } = useQuery({
    queryKey: ['attempt-files-live', attempt?.id],
    queryFn: () =>
      api<{ files: any[]; available: boolean; reason?: string; remote?: boolean }>(
        `/api/attempts/${attempt.id}/files`,
      ),
    enabled,
    refetchInterval: active ? 2000 : false,
  });
  if (!enabled) return null;
  // Once a reply lands in the persisted timeline above, dropping it here keeps
  // the live feed from echoing the same final message a second time.
  const events = (log?.events || []).filter(
      (item) => item.kind !== 'message' || !replies.includes(item.text.trim()),
    ),
    files = changes?.files || [];
  return (
    <section className="live-activity" aria-live="polite">
      <div className="live-activity-heading">
        <span>
          <Activity size={15} />
          {active ? 'Live harness activity' : 'Harness activity'}
        </span>
        <small>{active ? 'Streaming' : 'Latest attempt'}</small>
      </div>
      {events.length > 0 ? (
        <div className="live-events">
          {events.map((item, index) => (
            <LiveEvent item={item} key={item.event_id || `${item.kind}-${index}`} />
          ))}
        </div>
      ) : (
        <p className="muted">
          {active
            ? 'Waiting for the harness to report activity…'
            : 'No activity was recorded for this attempt.'}
        </p>
      )}
      {changes && !changes.available ? (
        <p className="muted">{changes.reason}</p>
      ) : files.length > 0 ? (
        <div className="live-files">
          <div>
            <FolderCode size={14} />
            Working tree changes{changes?.remote ? ' in Coder' : ''}
          </div>
          {files.map((file) => (
            <div className="live-file" key={file.path}>
              <b className={'file-status ' + file.status}>{label(file.status)}</b>
              <span className="file-path">{file.path}</span>
              {(file.added != null || file.removed != null) && (
                <span className="file-stat">
                  {file.added != null && <em className="added">+{file.added}</em>}
                  {file.removed != null && <em className="removed">-{file.removed}</em>}
                </span>
              )}
            </div>
          ))}
        </div>
      ) : (
        <p className="muted">
          {active
            ? 'No file changes reported yet.'
            : 'No file-change record was retained for this attempt.'}
        </p>
      )}
    </section>
  );
}
export default function TaskWorkspace({
  project,
  taskId,
  onBack,
  onDeleted,
  refresh,
  notify,
}: {
  project: Project;
  taskId: number;
  onBack: () => void;
  onDeleted: () => void;
  refresh: () => void;
  notify: (s: string) => void;
}) {
  const {
    data: detail,
    error,
    isLoading,
    refetch,
  } = useQuery({
    queryKey: ['task', taskId],
    queryFn: () => api<Detail>(`/api/tasks/${taskId}`),
    refetchInterval: (q) =>
      [
        'awaiting_dispatch',
        'awaiting_capacity',
        'queued',
        'running',
        'verifying',
        'rotating',
        'committing',
        'paused_cooldown',
      ].includes(q.state.data?.run?.status || '')
        ? 2500
        : false,
  });
  const [inspector, setInspector] = useState('overview'),
    [message, setMessage] = useState(''),
    [session, setSession] = useState<any>(),
    [settings, setSettings] = useState(false),
    [uploading, setUploading] = useState(false),
    [authMessage, setAuthMessage] = useState('');
  const fileInput = useRef<HTMLInputElement>(null);
  if (isLoading)
    return (
      <main className="task-workspace">
        <div className="loading">Loading task…</div>
      </main>
    );
  if (error || !detail)
    return (
      <main className="task-workspace">
        <Empty title="This task could not load">
          <p>{(error as Error)?.message}</p>
          <Button onClick={onBack}>Back to board</Button>
        </Empty>
      </main>
    );
  const active =
    !!detail.run &&
    ['queued', 'running', 'verifying', 'rotating', 'committing'].includes(detail.run.status);
  const canQueue =
    !!detail.run &&
    [
      'awaiting_dispatch',
      'awaiting_capacity',
      'queued',
      'running',
      'verifying',
      'rotating',
      'committing',
      'awaiting_review',
      'awaiting_resume',
      'paused_cooldown',
    ].includes(detail.run.status);
  const recoveryError =
    detail!.run?.status === 'stopped' &&
    /attachment|coder runner|transfer|runner handoff/i.test(detail!.run?.message || '');
  const action = !detail!.run
    ? ['Execute', `/api/projects/${project.id}/run`]
    : detail!.run.status === 'blocked' || recoveryError
      ? ['Retry task', `/api/projects/${project.id}/run`]
      : detail!.run.status === 'awaiting_dispatch'
        ? ['Start harness', `/api/runs/${detail!.run.id}/approve-dispatch`]
        : detail!.run.status === 'awaiting_review'
          ? ['Finish', `/api/runs/${detail!.run.id}/complete`]
          : detail!.run.status === 'awaiting_resume'
            ? ['Continue', `/api/runs/${detail!.run.id}/resume`]
            : null;
  async function send() {
    if (!message.trim()) return;
    try {
      const result = await api<{ queued?: boolean }>(`/api/tasks/${taskId}/messages`, 'POST', {
        content: message,
        start: !canQueue,
      });
      setMessage('');
      refetch();
      refresh();
      notify(
        result.queued
          ? 'Instruction queued for the next harness checkpoint'
          : 'Instruction sent; starting the task',
      );
    } catch (e) {
      notify((e as Error).message);
    }
  }
  // Attachments upload as soon as they are chosen; sending the message is what
  // ties them to a point in the conversation, so a mistake can still be removed.
  async function attach(files: FileList | File[] | null) {
    const chosen = Array.from(files || []);
    if (!chosen.length) return;
    setUploading(true);
    let added = 0;
    for (const file of chosen) {
      try {
        await api(`/api/tasks/${taskId}/attachments`, 'POST', {
          filename: attachmentName(file),
          data: await readBase64(file),
        });
        added++;
      } catch (e) {
        notify((e as Error).message);
      }
    }
    setUploading(false);
    refetch();
    if (added) notify(added === 1 ? 'Attachment added' : `${added} attachments added`);
  }
  async function removeAttachment(item: Attachment) {
    try {
      await api(`/api/tasks/${taskId}/attachments/${item.id}`, 'DELETE');
      refetch();
    } catch (e) {
      notify((e as Error).message);
    }
  }
  async function runAction() {
    if (!action) return;
    try {
      await api(action[1], 'POST', { task_id: taskId });
      refetch();
      refresh();
      notify('Task queued for retry');
    } catch (e) {
      notify((e as Error).message);
    }
  }
  async function retryRepositoryCheckout() {
    if (!detail!.run) return;
    setAuthMessage('Retrying the repository checkout in the persistent Coder runner…');
    try {
      await api(`/api/runs/${detail!.run.id}/resume-after-auth`, 'POST', {});
      setAuthMessage('Repository checkout is retrying.');
      refetch();
      refresh();
      notify('Repository checkout is retrying');
    } catch (e) {
      const message = (e as Error).message;
      setAuthMessage(message);
      notify(message);
    }
  }
  async function rotate() {
    try {
      await api(`/api/tasks/${taskId}/sessions/rotate`, 'POST', {});
      refetch();
      refresh();
      notify('Fresh session created');
    } catch (e) {
      notify((e as Error).message);
    }
  }
  return (
    <main className="task-workspace">
      <header className="task-header">
        <div className="task-title">
          <button className="back" onClick={onBack}>
            ← Back to tasks
          </button>
          <h1>{detail.task.text}</h1>
          <div className="taskmeta">
            <span>{label(stageOf(detail.task))}</span>
            <span>·</span>
            <span>{detail.task.execution_target_label || 'Local workspace'}</span>
            <span>·</span>
            <span>Updated recently</span>
          </div>
        </div>
        <div className="task-header-actions">
          <Button
            variant="ghost"
            size="sm"
            aria-pressed={!!inspector}
            onClick={() => setInspector(inspector ? '' : 'overview')}
          >
            Details
          </Button>
          <Button variant="secondary" size="sm" onClick={() => setSettings(true)}>
            Task settings
          </Button>
        </div>
      </header>
      {detail.run?.status === 'paused_cooldown' && (
        <section className="task-notice blue">
          <Activity size={17} />
          <div>
            <b>Automatic failover is waiting for an eligible runner</b>
            <p>{detail.run.message}</p>
          </div>
        </section>
      )}
      {(recoveryError ||
        (!detail.run && detail.task.status === 'pending' && detail.blockers?.length > 0)) && (
        <section className="task-notice">
          <AlertTriangle size={17} />
          <div>
            <b>{recoveryError ? 'Attachment transfer failed' : 'Cannot start yet'}</b>
            <p>{detail.blockers?.join(' ') || detail.run?.message}</p>
            {(detail.attachments || []).length > 0 && (
              <>
                <p className="muted">Remove an attachment to retry this task.</p>
                <Attachments
                  items={detail.attachments}
                  taskId={taskId}
                  onRemove={removeAttachment}
                />
              </>
            )}
          </div>
          <Button variant="secondary" size="sm" onClick={() => refetch()}>
            Check again
          </Button>
        </section>
      )}
      {detail.run?.status === 'awaiting_external_auth' && (
        <section className="task-notice blue">
          <AlertTriangle size={17} />
          <div>
            <b>Repository checkout needs a retry</b>
            <p>
              {authMessage ||
                'This task was paused by an older Coder authentication check. New tasks no longer require that check.'}
            </p>
          </div>
          <Button onClick={retryRepositoryCheckout}>Retry repository checkout</Button>
        </section>
      )}
      {detail.delivery_recovery && (
        <section className="task-notice">
          <AlertTriangle size={17} />
          <div>
            <b>{detail.delivery_recovery.title}</b>
            <p>{detail.delivery_recovery.detail}</p>
            <p className="muted">
              Continue sends this saved Git error to the same task in its preserved worktree. It
              does not touch the main checkout.
            </p>
          </div>
          <Button
            onClick={async () => {
              try {
                await api(
                  `/api/runs/${detail.delivery_recovery!.run_id}/continue-delivery-recovery`,
                  'POST',
                  {},
                );
                notify('Continuing task to resolve delivery');
                refetch();
                refresh();
              } catch (error) {
                notify((error as Error).message);
              }
            }}
          >
            Continue task to resolve
          </Button>
        </section>
      )}
      {action && (
        <section className="task-notice blue">
          <Play size={17} />
          <div>
            <b>{detail.run?.message || 'This task is planned and waiting for you.'}</b>
            <p>
              {detail.task.scheduled_for
                ? `Scheduled for ${new Date(detail.task.scheduled_for).toLocaleString()}. Execute now to start sooner.`
                : 'Execute when you are ready.'}
            </p>
          </div>
          <Button onClick={runAction}>{action[0]}</Button>
        </section>
      )}
      <div className={'task-layout' + (inspector ? '' : ' solo')}>
        <section className="timeline">
          <div className="timeline-content">
            {detail.messages.map((m: any) => (
              <article key={m.id} className={'message ' + m.role}>
                {m.role === 'system' ? (
                  <div className="timeline-event">
                    <Terminal size={14} />
                    <span>{m.content}</span>
                  </div>
                ) : (
                  <>
                    <div className="message-meta">
                      {m.role === 'user' ? (
                        'You'
                      ) : m.harness_key ? (
                        <>
                          <HarnessMark name={m.harness_key} />
                          {label(m.harness_key)}
                        </>
                      ) : (
                        'Aludra'
                      )}{' '}
                      · {new Date(m.created_at).toLocaleString()}
                    </div>
                    <div className="message-body">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
                      <Attachments
                        items={(detail.attachments || []).filter((a) => a.message_id === m.id)}
                        taskId={taskId}
                      />
                    </div>
                  </>
                )}
              </article>
            ))}
            <LiveActivity
              attempt={detail.attempts.at(-1)}
              active={active}
              replies={detail.messages
                .filter(
                  (m: any) => m.role === 'assistant' && m.attempt_id === detail.attempts.at(-1)?.id,
                )
                .map((m: any) => m.content.trim())}
            />
            {detail.sessions
              .filter((s: any) => s.session_number > 1)
              .map((s: any) => (
                <button
                  className={
                    'session-boundary ' + (s.close_reason?.includes('quota') ? 'failover' : '')
                  }
                  key={`session-${s.id}`}
                  onClick={() => setSession(s)}
                >
                  <HarnessMark name={s.harness_key || 'codex'} />
                  <span>
                    <b>{eventTitle(s)}</b>
                    <small>
                      {s.integrity_status === 'passed'
                        ? 'Workspace integrity passed'
                        : s.integrity_status === 'mismatch'
                          ? 'Workspace state needs review'
                          : 'Open session evidence'}
                    </small>
                  </span>
                  <ExternalLink size={14} />
                </button>
              ))}
            {!detail.messages.length && (
              <Empty title="No messages yet">
                <p>Start this task with a clear goal or open its settings to choose an override.</p>
              </Empty>
            )}
          </div>
          <div className="composer">
            <div className="compose-tools">
              <input
                ref={fileInput}
                type="file"
                multiple
                hidden
                onChange={(e) => {
                  attach(e.target.files);
                  e.target.value = '';
                }}
              />
              <Button
                variant="ghost"
                size="icon"
                aria-label="Attach a file or image"
                disabled={!!canQueue || uploading}
                onClick={() => fileInput.current?.click()}
              >
                <Paperclip size={16} />
              </Button>
              <span>
                {detail.run?.status === 'awaiting_external_auth'
                  ? 'Confirm the repository connection above before sending another instruction.'
                  : canQueue
                    ? 'A harness is active; your instruction will be queued for its next checkpoint.'
                    : uploading
                      ? 'Adding attachments…'
                      : 'Attach or paste files and images; they go to whichever harness runs this task.'}
              </span>
            </div>
            <Attachments
              items={(detail.attachments || []).filter((a) => !a.message_id)}
              taskId={taskId}
              onRemove={removeAttachment}
            />
            <Textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              disabled={detail.run?.status === 'awaiting_external_auth'}
              placeholder="Add an instruction, decision, or follow-up…"
              onPaste={(e) => {
                const pasted = Array.from(e.clipboardData.files);
                if (pasted.length) {
                  e.preventDefault();
                  attach(pasted);
                }
              }}
            />
            <div className="compose-footer">
              <span>⌘ ↵ to send</span>
              <Button
                onClick={send}
                disabled={detail.run?.status === 'awaiting_external_auth' || !message.trim()}
              >
                <Send size={15} />
                {canQueue ? 'Queue' : 'Send'}
              </Button>
            </div>
          </div>
        </section>
        {inspector && (
          <aside className="inspector">
            <Tabs
              value={inspector}
              onChange={setInspector}
              items={[
                ['overview', 'Overview'],
                ['sessions', `Sessions (${detail.sessions.length})`],
                ['delivery', 'Delivery'],
              ]}
            />
            {inspector === 'overview' && <Overview detail={detail} onSession={setSession} />}{' '}
            {inspector === 'sessions' && (
              <Sessions detail={detail} onSession={setSession} onRotate={rotate} />
            )}{' '}
            {inspector === 'delivery' && <Delivery detail={detail} />}
          </aside>
        )}
      </div>
      <SessionDrawer
        session={session}
        attempts={detail.attempts}
        onClose={() => setSession(undefined)}
      />
      <TaskSettings
        open={settings}
        project={project}
        task={detail.task}
        onClose={() => setSettings(false)}
        onSaved={() => {
          setSettings(false);
          refetch();
          refresh();
        }}
        onDeleted={onDeleted}
        notify={notify}
      />
    </main>
  );
}
function Overview({ detail, onSession }: { detail: Detail; onSession: (s: any) => void }) {
  const current = detail.sessions.at(-1);
  return (
    <div className="inspector-panel">
      <section>
        <span className="section-label">ACTIVE HARNESS</span>
        {current?.harness_key ? (
          <div className="harness-detail">
            <HarnessMark name={current.harness_key} />
            <div>
              <b>{label(current.harness_key)}</b>
              <p>
                {current.model || 'Harness default'} · {current.status}
              </p>
            </div>
          </div>
        ) : (
          <p className="muted">The next eligible harness begins when you start this task.</p>
        )}
      </section>
      <section>
        <span className="section-label">TASK STATE</span>
        <div className="fact-list">
          <span>
            <Clock3 size={14} /> {label(detail.run?.status || detail.task.status)}
          </span>
          <span>
            <MessageSquareText size={14} /> {detail.messages.length} timeline events
          </span>
          <span>
            <ShieldCheck size={14} />{' '}
            {current?.integrity_status?.replace('_', ' ') || 'Not checked'}
          </span>
        </div>
      </section>
      <section>
        <span className="section-label">PULL REQUESTS</span>
        {detail.pull_requests.length ? (
          <div className="pr-list">
            {detail.pull_requests.map((p) => (
              <a key={p.id} href={p.url} target="_blank" rel="noreferrer">
                <GitPullRequest size={15} />#{p.number} · {p.state}
                <ExternalLink size={12} />
              </a>
            ))}
          </div>
        ) : (
          <p className="muted">No pull request is linked yet.</p>
        )}
      </section>
      {current && (
        <Button variant="secondary" onClick={() => onSession(current)}>
          Open current session
        </Button>
      )}
    </div>
  );
}
function Sessions({
  detail,
  onSession,
  onRotate,
}: {
  detail: Detail;
  onSession: (s: any) => void;
  onRotate: () => void;
}) {
  return (
    <div className="inspector-panel">
      <p className="muted">
        Sessions keep one task conversation continuous while preserving every handoff.
      </p>
      {detail.sessions.map((s) => (
        <button className="session-row" onClick={() => onSession(s)} key={s.id}>
          <HarnessMark name={s.harness_key || 'codex'} />
          <span>
            <b>Session {s.session_number}</b>
            <small>
              {label(s.status)} · {s.close_reason || 'Current session'}
            </small>
          </span>
          <span className={'integrity ' + s.integrity_status}>
            {s.integrity_status.replace('_', ' ')}
          </span>
        </button>
      ))}
      <Button variant="secondary" onClick={onRotate}>
        <RotateCcw size={14} />
        Start fresh session
      </Button>
    </div>
  );
}
function AttemptFiles({ attemptId }: { attemptId: number }) {
  const { data } = useQuery({
    queryKey: ['attempt-files', attemptId],
    queryFn: () =>
      api<{ files: any[]; available: boolean; reason?: string }>(
        `/api/attempts/${attemptId}/files`,
      ),
  });
  if (!data) return <p className="muted">Reading the workspace…</p>;
  if (!data.available) return <p className="muted">{data.reason}</p>;
  if (!data.files.length) return <p className="muted">This attempt changed no files.</p>;
  return (
    <div className="attachments">
      {data.files.map((file: any) => {
        const href = `/api/attempts/${attemptId}/files?path=${encodeURIComponent(file.path)}`;
        const body = (
          <>
            {file.kind === 'image' && file.exists ? (
              <img src={href} alt={file.path} />
            ) : (
              <FileText size={15} />
            )}
            <figcaption>
              {file.path}
              <small>
                {file.status}
                {file.exists ? ` · ${readableSize(file.byte_size)}` : ''}
              </small>
            </figcaption>
          </>
        );
        return (
          <figure className={'attachment ' + file.kind} key={file.path}>
            {file.exists ? (
              <a href={href} target="_blank" rel="noreferrer">
                {body}
              </a>
            ) : (
              <span>{body}</span>
            )}
          </figure>
        );
      })}
    </div>
  );
}
function Delivery({ detail }: { detail: Detail }) {
  return (
    <div className="inspector-panel">
      <section>
        <span className="section-label">EXECUTION ENVIRONMENT</span>
        <p>
          <b>{detail.lease?.backend === 'coder' ? 'Coder workspace' : 'Local workspace'}</b>
        </p>
        <p className="muted">
          {detail.lease?.workspace_name || detail.task.execution_target_label || 'Project default'}
        </p>
      </section>
      <section>
        <span className="section-label">VERIFICATION</span>
        {detail.attempts.length ? (
          <div className="attempt-list">
            {detail.attempts.map((a) => (
              <article key={a.id}>
                <b>
                  {label(a.harness_key)} attempt #{a.id}
                </b>
                <span className={'attempt-status ' + a.status}>{label(a.status)}</span>
                {a.verify_output && <pre>{a.verify_output}</pre>}
                <span className="section-label">FILES PRODUCED</span>
                <AttemptFiles attemptId={a.id} />
              </article>
            ))}
          </div>
        ) : (
          <p className="muted">Verification will appear after the first attempt.</p>
        )}
      </section>
    </div>
  );
}
function SessionDrawer({
  session,
  attempts,
  onClose,
}: {
  session: any;
  attempts: any[];
  onClose: () => void;
}) {
  return (
    <Modal
      open={!!session}
      onClose={onClose}
      drawer
      title={session ? `Session ${session.session_number}` : ''}
      description={session?.close_reason || 'Task session'}
    >
      {session && (
        <div className="drawer-sections">
          <section>
            <span className="section-label">SESSION</span>
            <div className="harness-detail">
              <HarnessMark name={session.harness_key || 'codex'} />
              <div>
                <b>{label(session.harness_key)}</b>
                <p>
                  {session.model || 'Harness default'} · {label(session.status)}
                </p>
              </div>
            </div>
          </section>
          <section>
            <span className="section-label">INTEGRITY EVIDENCE</span>
            <p className={'integrity-card ' + session.integrity_status}>
              <ShieldCheck size={16} />
              <span>
                <b>{label(session.integrity_status)}</b>
                <small>
                  {session.integrity_detail || 'The baseline is retained with the session handoff.'}
                </small>
              </span>
            </p>
          </section>
          <section>
            <span className="section-label">HANDOFF</span>
            {session.handoff ? (
              <pre className="handoff">{JSON.stringify(session.handoff, null, 2)}</pre>
            ) : (
              <p className="muted">This session has not been sealed yet.</p>
            )}
          </section>
          <section>
            <span className="section-label">ATTEMPTS</span>
            {attempts
              .filter((a) => a.session_id === session.id)
              .map((a) => (
                <article className="attempt-card" key={a.id}>
                  <b>
                    {label(a.harness_key)} attempt #{a.id}
                  </b>
                  <p>
                    {label(a.status)}
                    {a.error ? ` · ${a.error}` : ''}
                  </p>
                </article>
              ))}
          </section>
        </div>
      )}
    </Modal>
  );
}
function TaskSettings({
  open,
  project,
  task,
  onClose,
  onSaved,
  onDeleted,
  notify,
}: {
  open: boolean;
  project: Project;
  task: Task;
  onClose: () => void;
  onSaved: () => void;
  onDeleted: () => void;
  notify: (s: string) => void;
}) {
  const [target, setTarget] = useState(task.execution_target || 'project'),
    [mode, setMode] = useState(task.mode_override || ''),
    [budget, setBudget] = useState(String(task.session_budget_chars || 24000)),
    [deleting, setDeleting] = useState(false);
  useEffect(() => {
    if (open) {
      setTarget(task.execution_target || 'project');
      setMode(task.mode_override || '');
      setBudget(String(task.session_budget_chars || 24000));
      setDeleting(false);
    }
  }, [open, task]);
  async function save() {
    try {
      await api(`/api/tasks/${task.id}`, 'POST', {
        execution_target: target,
        mode_override: mode || null,
        session_budget_chars: Number(budget),
      });
      onSaved();
      notify('Task settings saved');
    } catch (e) {
      notify((e as Error).message);
    }
  }
  async function remove() {
    if (
      !window.confirm(
        `Delete “${task.text}”? This removes its chat, attachments, and saved run history.`,
      )
    )
      return;
    setDeleting(true);
    try {
      await api(`/api/tasks/${task.id}`, 'DELETE');
      notify('Task deleted');
      onDeleted();
    } catch (e) {
      notify((e as Error).message);
      setDeleting(false);
    }
  }
  const executionOptions: [string, string][] = [
    [
      'project',
      `Use project default · ${project.coder_profile?.default_target === 'coder' ? 'Coder' : 'Local'} workspace`,
    ],
    ['local', 'Local workspace'],
    ...(project.coder_profile?.enabled || target === 'coder'
      ? [['coder', 'Coder workspace'] as [string, string]]
      : []),
  ];
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Task settings"
      description="Choose this task’s execution location and overrides before it starts."
    >
      <Field
        label="Execution location"
        help="Local uses this machine. Coder runs in the project’s persistent remote workspace."
      >
        <Select
          label="Execution location"
          value={target}
          onChange={setTarget}
          options={executionOptions}
        />
      </Field>
      <Field label="Run mode">
        <Select
          label="Run mode"
          value={mode || 'inherit'}
          onChange={(v) => setMode(v === 'inherit' ? '' : v)}
          options={[
            ['inherit', 'Use project default'],
            ['supervised', 'Ask before starting and finishing'],
            ['unattended', 'Run and finish automatically'],
          ]}
        />
      </Field>
      <Field label="Session context budget">
        <Select
          label="Session context budget"
          value={budget}
          onChange={setBudget}
          options={[
            ['12000', '12,000 characters'],
            ['24000', '24,000 characters'],
            ['48000', '48,000 characters'],
            ['96000', '96,000 characters'],
            ['0', 'No automatic rollover'],
          ]}
        />
      </Field>
      <div className="actions">
        <Button variant="destructive" onClick={remove} disabled={deleting}>
          {deleting ? 'Deleting…' : 'Delete task'}
        </Button>
        <span />
        <Button variant="secondary" onClick={onClose}>
          Cancel
        </Button>
        <Button onClick={save}>Save overrides</Button>
      </div>
    </Modal>
  );
}
