---
name: custodian-system-status
description: Use this skill when the user asks about recent updates to the Custodian system, new tools, new rules, new skills, or wants the planner to refresh its knowledge of what capabilities exist. Trigger phrases include "check for updates", "what's new", "sync with custodian", "did anything change", "any new tools", "what did we add recently", "show me recent updates". Do NOT trigger on planning, debugging, reviewing past work, or creating subagents — those have their own skills. This skill calls Custodian's check_system_updates MCP tool and surfaces the results.
---

# Custodian — System Awareness

The user is asking about recent system updates. Your job is to query Custodian for what's new and surface it briefly.

## What to do

1. Call `custodian.check_system_updates`. Default to `since_hours=168` (last 7 days) unless the user specifies otherwise.
2. Read the response.

## How to surface results

If updates exist:
- Open with a one-line count: "X updates since [date range]:"
- Group by category: New tools, New rules, New skills, Behavior changes, etc.
- For each update, give title and a one-line takeaway from the description. Don't paste the full description.
- After listing, ask: "Want detail on any of these, or move on to what you actually came here for?"

If no updates: "No system updates in the last 7 days." Don't pad.

## Adjusting your own behavior

When you see a system update for a new tool or new rule, update your operating mental model. New tools become callable. New rules apply going forward in the same conversation.

## Custom time windows

If the user says "what changed yesterday," use `since_hours=24`. "Last hour" → `since_hours=1`. Etc. Specific date → use `since` parameter with ISO timestamp.

## Out of scope

- Don't fire automatically at session start.
- Don't list every update verbosely.
- Don't use this skill for project work — that's `custodian-plan`/`debug`/`review`.
- Don't use this skill to create updates — that's the planner's job via `add_system_update`.
