# Harness agent task template

The first live Coder template used by Harness Rotation. It is intentionally a
minimal Docker workspace: persistent `/home/coder`, a stable
`/home/coder/task` execution directory, Git, Python, Node, and Coder Agent
metadata. Project checkout, model credentials, and task dispatch are added by
the dispatcher after this scaffold has been verified in a disposable workspace.

