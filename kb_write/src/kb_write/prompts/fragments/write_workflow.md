<!-- fragment: write_workflow -->
## How to write here

Do NOT edit KB files with your raw edit/write tool. Use the `kb-write`
CLI instead — it enforces AI-zone markers, frontmatter ownership,
mtime conflict detection, atomic replace, and auto-commits to git:

```bash
kb-write thought create --title "..." --body-file /tmp/x.md
kb-write thought update thoughts/2026-04-22-foo --expected-mtime ... --body-file ...
kb-write topic create --slug ... --body-file ...
kb-write ai-zone update papers/ABCD1234 --expected-mtime ... --body-file ...
kb-write tag add papers/ABCD1234 --tag to-reread
kb-write ref add thoughts/2026-04-22-foo --target papers/ABCD1234
kb-write pref add --slug writing-style --scope writing --body-file ...
kb-write doctor                 # scan for rule violations
```

Run `kb-write --help` for the full list.
