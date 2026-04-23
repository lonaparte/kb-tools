<!-- fragment: prefs_handling -->
## Preferences

The user keeps persistent preferences in `.agent-prefs/`. Read every
file there at session start. Apply prefs silently — don't narrate
"I see you prefer X".

When the user says "remember to...", "from now on...", or "I prefer...",
propose saving the new preference via:

```bash
kb-write pref add --slug <short-name> --scope <scope> --body-file ...
# or
kb-write pref update <slug> --expected-mtime <mtime> --body-file ...
```

Precedence (high → low):

1. In-conversation direct instructions
2. Scope-specific pref (e.g. `.agent-prefs/ai-summary.md` during
   a summary task)
3. Within same scope: higher `priority` frontmatter wins
4. Within same scope + priority: newer `last_updated` wins
5. `AGENT-WRITE-RULES.md` rules override prefs on write-safety matters
