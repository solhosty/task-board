# Harness Rotation

Harness Rotation tracks tasks while different coding harnesses work toward their outcomes.

## Language

**Project**:
A collection of related tasks with shared source and execution preferences.

**Task**:
A persistent objective whose conversation and work remain together across attempts.
_Avoid_: Runner, attempt

**Attempt**:
One harness's effort toward a task; a fallback starts another attempt on the same task.

**Runner**:
A persistent execution environment owned by a user, capable of serving multiple tasks.
_Avoid_: Task, session

**Task worktree**:
A task's separate working copy that retains changes between its attempts.
_Avoid_: Runner

**Concurrency limit**:
The maximum number of active tasks admitted to a runner, independent of how many tasks retain work there.

**Model connection**:
A user's authorization for a harness to use a model provider, distinct from authorization to access source repositories.
