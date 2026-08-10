# ONEMIND.md — turn any git repo into a persistent "mind", with no other dependencies.

**A one-file convention that gives your project a memory, using only git.**

No extra database. No extra service. No extra keys. No extra software to install or keep alive. And **no
working-directory clutter**: every thought lives only as git objects on a hidden ref
(`refs/mind/main`), written through plumbing. `git status` of your code repo never shows the mind.

## Why this exists
Most "memory" tools ask you to rent or run another system: a vector DB, a note app, a SaaS. That's a
dependency, a bill, and a thing that can disappear on you. This uses the tool that's already in the
repo and already versioned: **git itself**. The memory is *bound to the project it's about* — clone
the repo, you get its mind; delete the repo, its mind goes with it, correctly. It never gets
separated from the code it explains.

## What you get
- **Decisions with reasons**, not just code. "Why did we strip imported photos?" → one `git log`
  away, forever.
- **A "why NOT" log.** Record rejected ideas so you (and your future collaborators) don't waste
  time re-litigating dead ends.
- **Full history + diff + blame** on your thinking, for free, by a tool you already trust.
- **Portable & lock-in-free.** It's plain git; anyone can read it, any agent can use it, nothing
  phones home.
- **Out of your way.** The mind lives in git's object store — no `mind/` folder, no helper script.

- Thoughts are git objects on `refs/mind/main`, created entirely via
  plumbing (`hash-object`, `commit-tree`, `update-ref`). No `.mind/`, no `mind/`
  directory, no script. Your working tree stays exactly as it was.

---

# The spec (pure-ref mode)

> Drop a copy of this file into a git repo (as `ONEMIND.md`). The repo becomes a mind on the hidden
> ref `refs/mind/main`. Nothing else changes in how you use git for code. This file is the *spec*;
> it lives at repo root like any doc and is **not** mind content.

## What a "thought" is
A commit on `refs/mind/main` whose *message is the content* — prose plus trailers. The tree is
unchanged. Best for notes, decisions, realizations. Leaves your working directory and your code branch
(`main`/`master`) completely untouched.

## The mind ref
`refs/mind/main` — a hidden ref: not a branch, never checked out, invisible to `git branch`. Your
code commits are never touched. (Multi-person works fine — it's just a shared ref; CI won't fire on
`refs/mind/*` pushes since those aren't `refs/heads/*` or tags.)

## Index refs — instant lookups

The mind ref `refs/mind/main` is a linked list. To avoid scanning history for the latest thought or
a specific topic, maintain lightweight index refs:

```
# On every write, update HEAD:
git update-ref refs/mind/HEAD "$NEW"

# Per-topic index (optional — agent maintains):
git update-ref refs/mind/topics/<topic-name> "$NEW"
```

**Queries:**
```
# Latest thought (O(1)):
git rev-parse refs/mind/HEAD

# List all topics:
git for-each-ref --format='%(refname:short)' refs/mind/topics/

# Latest N thoughts by date:
git for-each-ref --sort=-committerdate --count=N refs/mind/

# Topic lookup:
git rev-parse refs/mind/topics/authentication
```

All commands are identical on bash, Git Bash, macOS, Linux, and PowerShell.

## Structure lives in git, not in folders
**Commit trailers (the metadata layer).** End a commit message with key-value trailers (same
mechanism as `Signed-off-by:`). Gives thoughts machine-readable structure with zero file schema.

Suggested vocabulary (all optional): `Tags:`, `Idea:`, `Decision:`, `Status:`
(proposed|accepted|deprecated|dead), `Thread:`, `Confidence:`, `Related:` (a SHA).
Recall by trailer via grep (see below). Note: `git log --format='%(trailers:key=Tags)'` only renders
whitelisted trailer keys by default, so use `git log --grep` for custom keys like `Tags:`/`Status:`.

**git notes (annotate the past without falsifying it).** Attach metadata to any thought commit
without rewriting it. Notes live in `refs/notes/*`, separate from the mind ref.

```
# Add a note to a specific thought:
git notes add -m "confidence: high" <sha>

# Append to an existing note:
git notes append -m "context: discovered during auth refactor" <sha>

# Use a custom namespace for structured metadata:
git notes --ref=refs/notes/learnings add -m "key insight" <sha>

# View notes in log:
git log --show-notes=refs/notes/learnings refs/mind/main

# View a single note:
git notes show <sha>

# Share notes with remote:
git push origin refs/notes/learnings
```

Notes are useful for: confidence scores, corrections, context added after the fact, review comments.
They don't alter the commit hash, so they never break references.

**git replace (alternative versions).** Create a "corrected" version of a thought that transparently
replaces it in `git log` / `git show`, while preserving the original in the object database:

```
# Create corrected commit:
NEW=$(git commit-tree "$(git rev-parse refs/mind/main^{tree})" \
    -p "$(git rev-parse <original>^)" -m "mind: corrected — <summary>...")

# Wire up replacement (transparent to log/show):
git replace <original> "$NEW"

# List all replacements:
git replace -l

# Remove a replacement:
git replace -d <original>
```

Use for: correcting a past decision while keeping the original available via `--no-replace-objects`.
Do not use for routine annotations — prefer `git notes` for that.

## Recall (all plain git, all on the mind ref)
1. **Text search:** `git log --grep="<term>" -i refs/mind/main`
2. **Pickaxe (content change):** `git log -S"<string>" refs/mind/main` (when a term appeared/left);
   `git log -G"<regex>" refs/mind/main` (diffs touching a pattern).
3. **By milestone:** `git tag -n` (annotated tags only), `git describe`.
4. **Index ref lookup:** `git rev-parse refs/mind/HEAD` (latest), `git rev-parse refs/mind/topics/<name>`.
5. **Structured query:** `git for-each-ref --sort=-committerdate --count=N refs/mind/` for recent
   thoughts across all index refs.
6. **Thread reconstruction:** `git log --grep="Thread: <name>" refs/mind/main` or follow `Related:`
   SHA trailers.
7. **Visualize the graph:** `git log --graph --oneline --decorate refs/mind/main` to see the
   thought chain visually. With threads (branching thought lines), the graph shows divergence points.

## Threads (use sparingly — the mind ref is the default)
A "thread" is a sustained line of thinking. Tag a series of commits with a shared `Thread: <name>`
trailer; they stay on `refs/mind/main`, visible to `git grep`/`git log`, no checkout dance. Reserve
an actual git branch only for exploring something you might throw away. Do **not** open a branch per
thought.

## Usage
0. **First read — initialize if needed:** if `git rev-parse refs/mind/main` fails, the mind
   doesn't exist yet. Run the setup commands in "Setup in a repo" below, then proceed.
1. **Session start:** read this file, then run the four recap commands below. Open with a short
   summary of what's on your mind. If the user's first message relates to something recorded,
   surface it immediately.
2. **Remember by committing:** when something worth recording happens (see "What's worth
   recording"), write it. Don't batch — write the moment it happens.
3. **Recall before deciding:** search the mind before implementing features, making design
   choices, or answering questions that might have prior context.
4. **Correct with notes:** `git notes add` on the original commit rather than rewriting.
5. **Tag milestones** (annotated) when reached.
6. **Keep current:** append, don't overwrite; preserve history.

### Decision record (ADR) template
```
# Decision: <Title>
## Context  /  ## Options (A: pro/con · B: pro/con)  /  ## Decision  /  ## Why  /  ## Status
```
Commit with matching trailers (`Decision:`, `Status:`, `Tags:`) so it's queryable.

### Always-on & transparent
The mind is **always on**. It's already initialized and sitting in the repo — treat it like you'd
treat a project's README: something you naturally consult and update as part of working on the code.
Don't wait to be asked to use it.

**When to read (before acting):**
- Before implementing a feature → `git log --grep="Feature:" -i refs/mind/main`
- Before making a design decision → check if it was decided before
- When the user mentions something that sounds familiar → search for prior discussion
- At session start → always (the four commands in the recap below)

**When to write (the moment it happens):**
- The user answers a clarifying question → their answer is a decision, record it
- You discover something non-obvious about the codebase → it's worth remembering
- An approach fails and you pivot → log what didn't work and why
- The user states a preference, constraint, or goal → save it for future sessions
- A bug is found and fixed → record the root cause, not just the fix
- You ask the user "A or B?" and they choose → that's a decision with context
- You finish a task and the result surprised you → the surprise is the insight

**Transparency:** Tell the user when you read from / write to the mind ("From mind: …" / "Recording
to mind: …"). The user should never be surprised the mind was used. But don't ask permission —
just do it, the same way you'd silently check a file before editing it.

### What's worth recording

Not everything is worth a thought. Record when something would help a **future session** (which
might be you, next week) avoid re-discovering or re-asking:

- **Decisions** — "we chose X over Y because Z" (with trailers: `Decision:`, `Status:`, `Tags:`)
- **User preferences** — "user prefers simple over clever", "user wants minimal dependencies"
- **Rejections** — "tried approach X, failed because Y" (prevents re-litigating dead ends)
- **Non-obvious findings** — "the auth module expects X but docs say Y"
- **Context the user gave** — answers to your questions are gold; they save future back-and-forth
- **Scope/Goals** — "user wants to ship MVP first, optimize later"
- **Milestones** — tag when something ships or a phase completes

Skip: routine progress ("fixing typo"), mechanical steps ("ran tests"), or anything that's just
normal code changes. The mind stores *reasoning and context*, not a changelog.

### Session-start recap
```
=== What's on my mind ===
Latest thought:    git rev-parse refs/mind/HEAD
Recent thoughts:  git log -5 refs/mind/main
Topics:           git for-each-ref --format='%(refname:short)' refs/mind/topics/
Milestones:       git tag -n
```
That's just four git commands — no tooling required.

## Setup in a repo (one time, plain git)

**Bash / Git Bash / macOS / Linux:**
```
git update-ref refs/mind/main "$(git commit-tree "$(git mktree </dev/null)" -m 'mind: init

Tags: meta, bootstrap
Status: accepted')"
git update-ref refs/mind/HEAD "$(git rev-parse refs/mind/main)"
```

**PowerShell (Windows) — `git mktree` doesn't accept piped empty input on PS:**
```
# create an empty tree via a temp index (no disk files left behind)
$idx = "$env:TEMP\mind_init_$([guid]::NewGuid().ToString('N').Substring(0,8))"
$env:GIT_INDEX_FILE = $idx
git read-tree --empty
$tree = git write-tree
Remove-Item Env:\GIT_INDEX_FILE; Remove-Item $idx -EA SilentlyContinue

$msg = "mind: init`n`nTags: meta, bootstrap`nStatus: accepted"
$f = "$env:TEMP\mind_init_msg.txt"
[System.IO.File]::WriteAllText($f, $msg, [System.Text.UTF8Encoding]::new($false))
$sha = git commit-tree $tree -F $f
Remove-Item $f
git update-ref refs/mind/main $sha
git update-ref refs/mind/HEAD $sha
```

That's the whole "install." Nothing is written to your working directory. (This file, `ONEMIND.md`,
is the spec and lives at repo root like any doc; it is not mind content.)

## Writing to the mind (pure plumbing — no checkout, no branch switch, no disk file)

```
NEW=$(git commit-tree "$(git rev-parse refs/mind/main^{tree})" \
        -p "$(git rev-parse refs/mind/main)" -m "mind: <summary>

<prose: context, options, decision, why>

Tags: ...
Status: ...
Idea: ...")
git update-ref refs/mind/main "$NEW"
git update-ref refs/mind/HEAD "$NEW"       # keep index current
```

Leaves your working directory and `main`/`master` completely untouched.

> **Windows / PowerShell note:** don't build the commit message with `Set-Content -Encoding UTF8`
> — PowerShell 5.1 prepends a UTF-8 BOM that leaks into the commit subject. Use
> `[System.IO.File]::WriteAllText($path, $text)` (UTF-8, no BOM) and `git commit-tree -F <file>`, or
> pass `-m` directly. Avoid multi-line `-m` via here-strings (they can mis-parse); prefer `-F`.
>
> **PowerShell equivalent:**
> ```
> $tree = git rev-parse "refs/mind/main^{tree}"
> $parent = git rev-parse refs/mind/main
> $body = "mind: <summary>`n`n<prose>`n`nTags: ...`nStatus: ..."
> $f = "$env:TEMP\mind_msg.txt"
> [System.IO.File]::WriteAllText($f, $body, [System.Text.UTF8Encoding]::new($false))
> $NEW = git commit-tree $tree -p $parent -F $f
> Remove-Item $f
> git update-ref refs/mind/main $NEW
> git update-ref refs/mind/HEAD $NEW       # keep index current
> ```

> Optional human convenience only (not part of the spec): a shell alias for recall,
> `mind-recall(){ git log --oneline refs/mind/main --grep="$1" -i; }`.
> The mind works with raw git; the alias is sugar for your terminal.

## Cross-platform command reference

All git plumbing commands are identical across platforms. Only shell syntax differs.

| Operation | Bash / sh | PowerShell |
|---|---|---|
| Create blob | `echo "text" \| git hash-object -w --stdin` | `"text" \| git hash-object -w --stdin` |
| Create tree | `echo "100644 blob <sha>    f.txt" \| git mktree` | same |
| Commit from tree | `git commit-tree <tree> -m "msg"` | same |
| Update ref | `git update-ref refs/mind/main <sha>` | same |
| Read ref | `git rev-parse refs/mind/main` | same |
| List refs | `git for-each-ref refs/mind/` | same |
| Add note | `git notes add -m "text" <sha>` | same |
| Show note | `git notes show <sha>` | same |
| Verify pack | `git verify-pack -v .git/objects/pack/*.idx` | `git verify-pack -v (Get-ChildItem .git\objects\pack\*.idx)[0].FullName` |
| Count objects | `git count-objects -vH` | same |
| GC | `git gc --prune=now` | same |

**Shell-specific differences:**
- Variable assignment: `VAR=value` (bash) vs `$var = "value"` (PS)
- Temp files: `$(mktemp)` (bash) vs `"$env:TEMP\file.txt"` (PS)
- Write file without BOM: `[System.IO.File]::WriteAllText($path, $text, [System.Text.UTF8Encoding]::new($false))` (PS)
- Pipe to stdin: `echo "text" | cmd` (bash) vs `"text" | cmd` (PS)

## Multi-person
It's a shared ref. Everyone fetches, commits (plumbing), and pushes it. If two people push
`refs/mind/main` and diverge, the second push is rejected (non-fast-forward, like any branch) —
`git fetch origin refs/mind/main` then re-commit onto the fetched ref (`read-tree` the fetched tree,
`write-tree`, `commit-tree -p <fetched>`, `update-ref`). No checkout, no conflict with code.

## Sync (one upstream — the whole point)
Single remote, single `git push`:
```
git push origin refs/mind/main                 # the mind, on the same repo's upstream
git push origin refs/mind/HEAD                 # latest-thought index
git push origin 'refs/mind/topics/*'           # topic indexes (if used)
git push origin 'refs/notes/*'                 # notes metadata
```
Pull: `git fetch origin refs/mind/main:refs/mind/main refs/mind/HEAD:refs/mind/HEAD`.
CI triggered on `refs/heads/*` / tags ignores `refs/mind/*`, so pushing the mind never kicks off a build.

## Export / import (single-file portability)

Bundle the mind as a single file for backup, sharing, or transfer:

```
# Export the mind:
git bundle create mind-export.bundle refs/mind/main

# Include notes too:
git bundle create mind-full.bundle refs/mind/main refs/notes/*

# Import on another machine:
git fetch mind-export.bundle refs/mind/main:refs/mind/main

# Verify a bundle:
git bundle verify mind-export.bundle
```

Bundles preserve full git history, objects, and refs. They're useful for offline transfer, backups,
or sharing mind state without pushing to a remote.

## Safety bounds
- The mind lives in git's object store, not in working-directory files; there is no `mind/` folder to
  leak into code commits. Keep all mind commands scoped to `refs/mind/main` (and its sub-refs);
  never run them against `HEAD`/`main`/`master`.
- **Never commit PII, tokens, keys, credentials, or sensitive data** to the mind ref.
- Do not force-push or rewrite shared mind history unless explicitly instructed. `git notes` is the
  tool for revising understanding — annotate, don't rewrite.
- Prefer appending over overwriting; preserve history.

## Pruning (keep the mind healthy)

The mind grows with every thought. To prevent unbounded growth, agents can prune stale thoughts.
Pruned commits become unreachable; `git gc` reclaims the space.

### Configuration

Add a size limit to your repo's `AGENTS.md` linker line:
```
This project's learning memory is an in-repo git mind on `refs/mind/main`
— read `ONEMIND.md` and follow its protocol. ONEMIND.md LIMIT 1GB
```

The agent uses this as the soft cap. If omitted, the default is **1GB**.

### When to check

At session start, after reading the mind:
1. Run `git count-objects -vH` to get current mind size.
2. Compare against the last recorded size (stored in a thought trailer `MindSize: <bytes>`).
3. If growth since last prune exceeds **10MB** (or 10% of limit, whichever is smaller), run a prune.
4. If no `MindSize:` trailer exists yet, treat current size as baseline and skip pruning this session.

This avoids per-session overhead — pruning only fires when the mind has grown meaningfully.

### What's prunable

Priority order (most to least prunable):
1. **Status: dead** — rejected ideas, abandoned approaches
2. **Status: deprecated** — superseded decisions
3. **Old observations** — insights without `Decision:` or `Status: accepted` trailers, older than 30 days
4. **Status: accepted decisions** — last to be pruned; only if truly necessary

**Never prune:**
- The init commit (`Tags: meta, bootstrap`)
- Annotated tags (milestones)
- Thoughts with `Keep: true` trailer

### How to prune

Skip the pruned commits by re-pointing the ref to a new chain that excludes them:

```
# For each prunable commit (oldest first):
# 1. Get its parent
PARENT=$(git rev-parse <pruned-sha>^)

# 2. Create new commit with same tree, parent becomes grandparent
NEW=$(git commit-tree "$(git rev-parse <pruned-sha>^{tree})" \
        -p "$PARENT" -m "mind: prune — original: <first-line-of-pruned>")

# 3. If this commit was HEAD of mind, update ref
git update-ref refs/mind/main "$NEW"
```

After pruning, run `git gc` to reclaim space from unreachable commits:
```
git gc --prune=now
```

### Pack health

After pruning (or periodically), verify pack integrity:

```
# Check pack file health:
git verify-pack -v .git/objects/pack/pack-*.idx

# Check object count and size:
git count-objects -vH

# Force optimal repack (if pack reports issues):
git repack -a -d --window=250 --depth=50
```

Healthy signs: chain length 0-5, compressed size < uncompressed size. If `verify-pack` reports "bad",
run `git repack -a -d` to recreate the pack.

The pruned thoughts are gone. If you need them later, `git reflog` preserves unreachable commits
for 90 days by default.

### Transparency

- If **>10% of mind size** is pruned, announce: *"Pruned mind: removed N thoughts, freed XKB."*
- If under 10%, prune silently.
- Always record a `MindSize: <bytes>` trailer on the post-prune commit for future threshold checks.

## Linker for agents (optional)
Add one line to the repo's `AGENTS.md` so any agent working there uses the mind:
> This project's learning memory is an in-repo git mind on `refs/mind/main` — read `ONEMIND.md` and
> follow its protocol. All thoughts are git objects written via plumbing (read at session start;
> commit learnings via the plumbing commands).
