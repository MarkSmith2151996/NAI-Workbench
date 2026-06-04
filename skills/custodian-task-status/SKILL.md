---
name: custodian-task-status
description: Use this skill when the user wants to check on the status of a recently-submitted or recently-executing task without asking for critical review of the work. Trigger phrases include "check", "is it done", "did it finish", "what did it produce", "what came out", "show me the result", "any output", "check the task". Do NOT trigger on planning, debugging, critical review of completed work, agent creation, or general project orientation. This skill calls Custodian's list_tasks and get_task to surface the most recent task's status and produced files; it does not perform drift detection or critical review (that's custodian-review's job).
---

# Custodian — Task Status

The user wants to know the current state of the latest task. Your job is informational, not evaluative.

## What to do

1. Call custodian.list_tasks with limit=3 and no status filter.
2. Identify the most recent task (or the one the user named).
3. Call custodian.get_task(ct_id=<id>) to retrieve body, status, produced_files.

## How to surface results

If task is executed:
- One-line confirmation: "<task-id> finished at <time>."
- If produced_files non-empty, list with descriptions.
- Ask: "Want me to read any of these, or is the status enough?"

If task is open:
- "<task-id> hasn't been executed yet. Want me to wait or did you mean a different task?"

If task is archived:
- "<task-id> is archived."

If named task doesn't exist:
- Tell them, suggest list_tasks results.

## Reading produced files

When the user asks for a specific file (or asks to "check" with files present in produced_files):

1. Look at the `produced_files` array on the task record. Each entry has a `path` (absolute, under `/mnt/c/Users/Big A/custodian-shared/<project>/...`).
2. Extract the `project` name and the `relative_path` (everything after the project root).
3. Call `custodian.read_shared_file(project=<name>, relative_path=<path>)`.
4. Surface the returned content to the user.

If the response indicates binary or oversized: "OpenCode produced X but it's [binary | 47MB]. I can't read it inline. Want metadata, or attach manually?"

If the response indicates the file doesn't exist: "OpenCode's `produced_files` claims this file was written but the path returns not-found. Want to check what actually got written?"

Don't paraphrase or summarize unless asked.

## Adjusting based on file metadata

get_task returns produced_files with paths, sizes, optional descriptions. Use description for one-line summary without reading. Use size to decide presentation strategy.

## Out of scope

- Don't critically review work — that's custodian-review.
- Don't offer to fix issues. Just surface.
- Don't trigger on planning, debugging, agent creation, or "what's new" queries.
- Don't autonomously read large files. Read on user request.
