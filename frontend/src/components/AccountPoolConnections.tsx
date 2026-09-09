import { useEffect, useMemo, useState } from 'react';
import { CheckCircle2, ExternalLink, Plus, Trash2 } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { api, type Bootstrap } from '../lib/api';
import { Button } from './ui/button';
import { Field, Input, Modal, Select } from './ui/controls';
import { HarnessMark } from './BrandMark';

type Provider = 'codex' | 'claude';
type Runner = { id: number; workspace_name: string; available?: boolean };
type GitHubStatus = {
  authenticated: boolean;
  login_url: string;
  installation_count: number;
  display_name: string;
};
type Login = {
  runner: Runner;
  provider: Provider;
  verification_url?: string;
  user_code?: string;
  screen?: string;
};

export default function AccountPoolConnections({
  data,
  refresh,
  notify,
}: {
  data: Bootstrap;
  refresh: () => void;
  notify: (message: string) => void;
}) {
  const bindings = data.account_bindings || [];
  const runners = (data.runners || []) as Runner[];
  const bound = new Set(bindings.map((item: any) => Number(item.runner_id)));
  const stale = runners.filter((r) => r.available === false);
  const reusable = runners.filter((r) => r.available !== false && !bound.has(r.id));
  const server = data.coder_servers[0];
  const [setup, setSetup] = useState(false),
    [provider, setProvider] = useState<Provider>('codex'),
    [runnerId, setRunnerId] = useState('new'),
    [working, setWorking] = useState(false);
  const [provisioningJob, setProvisioningJob] = useState<string>();
  const [login, setLogin] = useState<Login>();
  const [claudeCode, setClaudeCode] = useState('');
  const [claudeAwaiting, setClaudeAwaiting] = useState(false);
  const github = useQuery({
    queryKey: ['github-setup', server?.id],
    enabled: Boolean(server && setup),
    queryFn: () => api<GitHubStatus>(`/api/coder-servers/${server.id}/external-auth/github`),
  });
  const githubReady = github.data?.authenticated === true;
  useEffect(() => {
    if (runnerId !== 'new' && !reusable.some((r) => String(r.id) === runnerId)) setRunnerId('new');
  }, [runnerId, reusable]);
  useEffect(() => {
    if (!provisioningJob) return;
    const timer = window.setInterval(async () => {
      try {
        const result = await api<{ job: any }>(`/api/runner-provisioning/${provisioningJob}`);
        if (result.job.status === 'ready') {
          window.clearInterval(timer);
          setProvisioningJob(undefined);
          await beginSignIn(result.job.runner);
        } else if (result.job.status === 'failed') {
          window.clearInterval(timer);
          setProvisioningJob(undefined);
          setWorking(false);
          notify(result.job.message);
        }
      } catch (error) {
        window.clearInterval(timer);
        setProvisioningJob(undefined);
        setWorking(false);
        notify((error as Error).message);
      }
    }, 2000);
    return () => window.clearInterval(timer);
  }, [provisioningJob]);
  useEffect(() => {
    if (!login || login.provider !== 'claude' || login.verification_url) return;
    const timer = window.setInterval(async () => {
      try {
        const result = await api<{ flow?: { verification_url?: string; screen?: string } }>(
          `/api/coder-runners/${login.runner.id}/model-auth/claude/flow`,
        );
        if (result.flow)
          setLogin((current) =>
            current
              ? {
                  ...current,
                  verification_url: result.flow?.verification_url || current.verification_url,
                  screen: result.flow?.screen || current.screen,
                }
              : current,
          );
      } catch {}
    }, 1000);
    return () => window.clearInterval(timer);
  }, [login?.runner.id, login?.provider, login?.verification_url]);
  useEffect(() => {
    if (!login || login.provider !== 'claude' || !claudeAwaiting) return;
    const timer = window.setInterval(async () => {
      try {
        const result = await api<any>(`/api/coder-runners/${login.runner.id}/model-auth/claude`);
        if (!result.status?.authenticated) return;
        const display = 'Claude Code';
        await api('/api/account-bindings', 'POST', {
          provider: 'claude',
          label: `${display} · ${result.status.email || login.runner.workspace_name}`,
          runner_id: login.runner.id,
        });
        window.clearInterval(timer);
        setClaudeAwaiting(false);
        setLogin(undefined);
        notify('Claude account added to the pool.');
        refresh();
      } catch {}
    }, 1500);
    return () => window.clearInterval(timer);
  }, [login?.runner.id, login?.provider, claudeAwaiting]);
  const title = provider === 'codex' ? 'Codex' : 'Claude Code';
  const setupOptions = useMemo(
    () => [
      ['new', 'Create a new private runner'] as [string, string],
      ...reusable.map((r) => [String(r.id), r.workspace_name] as [string, string]),
    ],
    [reusable],
  );
  async function removeConnection(id: number) {
    try {
      await api(`/api/account-bindings/${id}`, 'DELETE');
      notify('Connection removed from the pool.');
      refresh();
    } catch (error) {
      notify((error as Error).message);
    }
  }
  async function removeStale(runner: number, withConnection = false) {
    try {
      await api(
        `/api/coder-runners/${runner}${withConnection ? '?remove_connection=1' : ''}`,
        'DELETE',
      );
      notify(
        withConnection
          ? 'Unavailable connection and runner record removed.'
          : 'Unavailable runner record removed.',
      );
      refresh();
    } catch (error) {
      notify((error as Error).message);
    }
  }
  async function beginSignIn(runner: Runner) {
    const flow = await api<any>(
      `/api/coder-runners/${runner.id}/model-auth/${provider}/connect`,
      'POST',
      {},
    );
    if (flow.verification_url) window.open(flow.verification_url, '_blank', 'noopener');
    setSetup(false);
    setWorking(false);
    setLogin({
      runner,
      provider,
      verification_url: flow.verification_url,
      user_code: flow.user_code,
    });
    refresh();
  }
  async function finishSignIn() {
    if (!login) return;
    setWorking(true);
    try {
      const result = await api<any>(
        `/api/coder-runners/${login.runner.id}/model-auth/${login.provider}`,
      );
      if (!result.status?.authenticated) {
        notify(
          'The sign-in is not complete yet. Finish it in the provider window, then check again.',
        );
        return;
      }
      const display = login.provider === 'codex' ? 'Codex' : 'Claude Code';
      await api('/api/account-bindings', 'POST', {
        provider: login.provider,
        label: `${display} · ${result.status.email || login.runner.workspace_name}`,
        runner_id: login.runner.id,
      });
      setLogin(undefined);
      notify('Account added to the pool.');
      refresh();
    } catch (error) {
      notify((error as Error).message);
    } finally {
      setWorking(false);
    }
  }
  async function submitClaudeCode(value = claudeCode) {
    const code = value.trim();
    if (!login || !code) return;
    setWorking(true);
    try {
      await api(`/api/coder-runners/${login.runner.id}/model-auth/claude/input`, 'POST', {
        input: code,
      });
      setClaudeCode('');
      setClaudeAwaiting(true);
      notify('Claude code sent. Finishing sign-in automatically…');
    } catch (error) {
      notify((error as Error).message);
    } finally {
      setWorking(false);
    }
  }
  async function start() {
    if (!server) return notify('Connect Coder before adding an account.');
    setWorking(true);
    try {
      if (runnerId === 'new') {
        const result = await api<{ job: { id: string } }>(
          `/api/coder-servers/${server.id}/runners/create`,
          'POST',
          {},
        );
        setProvisioningJob(result.job.id);
        return;
      }
      const selected = reusable.find((r) => String(r.id) === runnerId);
      if (!selected) throw new Error('That runner is no longer available. Refresh and try again.');
      await beginSignIn(selected);
    } catch (error) {
      notify((error as Error).message);
      setWorking(false);
    }
  }
  return (
    <div className="connections-page">
      <div className="connections-hero">
        <div>
          <p className="eyebrow">Execution capacity</p>
          <h1>Account pool</h1>
          <p>
            Add one paid account per private runner. The scheduler uses this order when an account
            is unavailable or out of usage.
          </p>
        </div>
        <div className="pool-stat">
          <b>{bindings.filter((b: any) => b.enabled).length}</b>
          <span>ready accounts</span>
        </div>
      </div>
      <section className="pool-card">
        <header>
          <div>
            <h2>Pool accounts</h2>
            <p>Manage failover order in Settings → Sessions.</p>
          </div>
          <Button onClick={() => setSetup(true)}>
            <Plus size={15} />
            Add account
          </Button>
        </header>
        {bindings.length ? (
          <div className="account-list">
            {bindings.map((item: any, index: number) => {
              const runner = runners.find((r) => r.id === item.runner_id);
              const unavailable = runner?.available === false;
              return (
                <article className="account-row" key={item.id}>
                  <span className="account-rank">{index + 1}</span>
                  <HarnessMark name={item.provider} />
                  <div>
                    <b>{item.label}</b>
                    <p>{item.workspace_name}</p>
                  </div>
                  <span
                    className={'account-state ' + (!unavailable && item.enabled ? 'ready' : '')}
                  >
                    {unavailable ? 'Needs cleanup' : item.enabled ? 'Ready' : 'Paused'}
                  </span>
                  <Button variant="ghost" size="sm" onClick={() => removeConnection(item.id)}>
                    Remove
                  </Button>
                </article>
              );
            })}
          </div>
        ) : (
          <div className="pool-empty">
            <b>No accounts yet</b>
            <p>
              Start by creating a runner, then sign in to the provider account you want the pool to
              use.
            </p>
            <Button onClick={() => setSetup(true)}>
              <Plus size={15} />
              Set up first account
            </Button>
          </div>
        )}
      </section>
      {stale.length > 0 && (
        <section className="unassigned-card cleanup-card">
          <header>
            <div>
              <p className="eyebrow">Recovery</p>
              <h2>Unavailable runners</h2>
              <p>
                These local entries no longer exist in Coder. Removing them does not delete any
                remote workspace.
              </p>
            </div>
          </header>
          <div className="account-list">
            {stale.map((r) => (
              <article className="account-row" key={r.id}>
                <span className="account-rank">!</span>
                <div>
                  <b>{r.workspace_name}</b>
                  <p>
                    {bound.has(r.id)
                      ? 'Its pool connection will be removed with this stale record.'
                      : 'Safe to remove from this dashboard.'}
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => removeStale(r.id, bound.has(r.id))}
                >
                  <Trash2 size={14} />
                  {bound.has(r.id) ? 'Remove connection + record' : 'Remove record'}
                </Button>
              </article>
            ))}
          </div>
        </section>
      )}
      {setup && (
        <Modal
          open
          title="Add a pool account"
          description="A fresh runner is isolated: it holds one provider login and uses your configured GitHub installation."
          onClose={() => !working && setSetup(false)}
        >
          <div className="setup-steps">
            <span className={githubReady ? 'ready' : ''}>
              {githubReady && <CheckCircle2 size={15} />}1. GitHub
            </span>
            <span className={provisioningJob ? 'ready' : ''}>
              {provisioningJob && <CheckCircle2 size={15} />}2. Runner
            </span>
            <span>3. Provider sign-in</span>
          </div>
          {provisioningJob ? (
            <div className="setup-warning">
              <p>
                Coder is preparing your private runner in the background. You can keep using the
                app; this step will continue automatically when it is ready.
              </p>
            </div>
          ) : (
            <>
              <Field
                label="GitHub installation"
                help="GitHub access is configured once in Coder and inherited by new runners."
              >
                {github.isLoading ? (
                  <p className="muted">Checking Coder GitHub access…</p>
                ) : githubReady ? (
                  <p className="setup-ok">
                    <CheckCircle2 size={15} /> {github.data?.display_name} connected
                    {github.data?.installation_count
                      ? ` · ${github.data.installation_count} installation${github.data.installation_count === 1 ? '' : 's'}`
                      : ''}
                  </p>
                ) : (
                  <div className="setup-warning">
                    <p>
                      {github.isError
                        ? 'Coder could not verify GitHub access. Reconnect it in Coder, then reopen this step.'
                        : 'Connect GitHub in Coder before creating a runner that needs repository access.'}
                    </p>
                    {github.data?.login_url && (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => window.open(github.data?.login_url, '_blank', 'noopener')}
                      >
                        <ExternalLink size={14} />
                        Connect GitHub
                      </Button>
                    )}
                  </div>
                )}
              </Field>
              <Field label="Provider">
                <Select
                  label="Provider"
                  value={provider}
                  onChange={(value) => setProvider(value as Provider)}
                  options={[
                    [`codex`, `Codex`],
                    [`claude`, `Claude Code`],
                  ]}
                />
              </Field>
              <Field label="Runner">
                <Select
                  label="Runner"
                  value={runnerId}
                  onChange={setRunnerId}
                  options={setupOptions}
                />
              </Field>
              <div className="actions">
                <Button variant="secondary" disabled={working} onClick={() => setSetup(false)}>
                  Cancel
                </Button>
                <Button disabled={working || github.isLoading || !githubReady} onClick={start}>
                  {working
                    ? 'Starting…'
                    : runnerId === 'new'
                      ? `Create runner and sign in to ${title}`
                      : `Use runner and sign in to ${title}`}
                </Button>
              </div>
            </>
          )}
        </Modal>
      )}
      {login && (
        <Modal
          open
          title={`Sign in to ${login.provider === 'codex' ? 'Codex' : 'Claude Code'}`}
          description={`This sign-in is isolated to ${login.runner.workspace_name}. When you finish, it will be added to the pool.`}
          onClose={() => !working && !claudeAwaiting && setLogin(undefined)}
        >
          {login.user_code ? (
            <div className="setup-warning">
              <p>
                Enter this one-time code in the provider window: <strong>{login.user_code}</strong>
              </p>
              {login.verification_url && (
                <Button
                  variant="secondary"
                  size="sm"
                  onClick={() => window.open(login.verification_url, '_blank', 'noopener')}
                >
                  <ExternalLink size={14} />
                  Open sign-in
                </Button>
              )}
            </div>
          ) : login.provider === 'claude' ? (
            <>
              {claudeAwaiting ? (
                <div className="setup-warning">
                  <p>
                    Claude is finishing sign-in in the runner. This closes automatically once the
                    account is ready.
                  </p>
                </div>
              ) : (
                <>
                  <div className="setup-warning">
                    <p>
                      {login.verification_url
                        ? 'Open Claude, finish the browser login, then paste the returned code below. Pasting submits it automatically.'
                        : 'Preparing the secure Claude sign-in link…'}
                    </p>
                    {login.verification_url && (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => window.open(login.verification_url, '_blank', 'noopener')}
                      >
                        <ExternalLink size={14} />
                        Open Claude sign-in
                      </Button>
                    )}
                  </div>
                  <Field
                    label="Claude callback code"
                    help="Paste the code Claude shows after browser login. The app sends it to this runner and finishes automatically."
                  >
                    <Input
                      value={claudeCode}
                      onChange={(event) => setClaudeCode(event.target.value)}
                      onPaste={(event) => {
                        const code = event.clipboardData.getData('text').trim();
                        if (code) {
                          event.preventDefault();
                          setClaudeCode(code);
                          window.setTimeout(() => submitClaudeCode(code), 0);
                        }
                      }}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.preventDefault();
                          submitClaudeCode();
                        }
                      }}
                      placeholder="Paste code from Claude"
                    />
                  </Field>
                  <Button
                    variant="secondary"
                    disabled={working || !claudeCode.trim()}
                    onClick={() => submitClaudeCode()}
                  >
                    Submit code
                  </Button>
                </>
              )}
            </>
          ) : (
            <p className="muted">
              Complete the provider sign-in in its opened window, then return here.
            </p>
          )}
          <div className="actions">
            <Button
              variant="secondary"
              disabled={working || claudeAwaiting}
              onClick={() => setLogin(undefined)}
            >
              Cancel
            </Button>
            {login.provider !== 'claude' && (
              <Button disabled={working} onClick={finishSignIn}>
                {working ? 'Checking…' : 'I’ve signed in'}
              </Button>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}
