"""Run Claude Code under a PTY while forwarding stdin/stdout over Coder SSH.

The desktop dashboard owns only this short-lived terminal transport. Claude Code
performs the login and writes its credentials in the runner's own home directory.
"""
import os
import pty
import select
import sys


def main():
    child, terminal = pty.fork()
    if child == 0:
        os.execv('/home/coder/.local/node_modules/.bin/claude', ['claude'])
    while True:
        readable, _, _ = select.select([terminal, sys.stdin.fileno()], [], [])
        if terminal in readable:
            try:
                data = os.read(terminal, 4096)
            except OSError:
                break
            if not data:
                break
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        if sys.stdin.fileno() in readable:
            data = os.read(sys.stdin.fileno(), 4096)
            if not data:
                break
            os.write(terminal, data)
    _, status = os.waitpid(child, 0)
    raise SystemExit(os.waitstatus_to_exitcode(status))


if __name__ == '__main__':
    main()
