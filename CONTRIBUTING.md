# Contributing

Thanks for taking the time to contribute to Harness Rotation.

## Before opening a pull request

1. Keep a change focused on one behavior or maintenance concern.
2. Add or update tests when behavior changes.
3. Run the relevant checks from the repository root:

       npm run test:python
       npm --prefix frontend test
       npm run format:check

4. Build the frontend when changing it:

       npm run build

Do not commit .harness/, credentials, local logs, generated frontend output, or provider transcripts. Do not include secrets in issues, pull requests, test fixtures, or commit messages.

## Design and safety expectations

Keep the distinction between task state, execution state, and provider state explicit. A local working directory and a Coder worktree are not security sandboxes. Changes that affect credentials, command construction, remote transport, or Git delivery require focused tests and a clear explanation of their safety boundary.

## Reporting bugs

Include the operating system, Python and Node versions, the harness involved, minimal reproduction steps, and sanitized logs. Use the security process for vulnerabilities.
