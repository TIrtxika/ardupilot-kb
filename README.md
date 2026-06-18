# ardupilot-kb — Claude Code scaffold for a local ArduPilot knowledge system

This is a `.claude/` configuration for **building** a fully local, offline, domain-decomposed
knowledge system over the ArduPilot source repo + wiki. Claude Code is the build/maintenance
tool; the resulting system runs on local models only (Ollama + local embeddings + LanceDB),
with no cloud dependency at runtime.

## Quick start

1. Install Claude Code (`npm i -g @anthropic-ai/claude-code`) and run `claude` in this folder.
2. Local runtime prerequisites (for the system you are building, not for Claude Code itself):
   Ollama with a code model (e.g. `ollama pull qwen3:27b`), an embedding model
   (Qwen3-Embedding-8B or BGE-M3), Python with tree-sitter + DuckDB + LanceDB.
3. Drive it with the slash commands, in phase order:
   - `/ingest`        Phase 0/1 — corpus + deterministic graph/params/messages
   - `/eval build`    Phase 0   — author the gold eval set
   - `/index-domain`  Phase 2   — global hybrid index, then measure
   - `/index-domain <domain>`  Phase 3 — per-domain indices + router
   - `/ask <question>`         query path (route -> retrieve -> ground -> audit)
   - `/eval`          measure any change; nothing ships without a number

## What's inside

```
CLAUDE.md                  project rules (read this first)
.claude/
  settings.json            scoped permissions + env defaults
  agents/                  7 subagents (ingest, graph, params, index, route, audit, eval)
  skills/                  5 skills (domains, chunking, param-parsing, grounding, eval)
  commands/                4 slash commands (ingest, index-domain, ask, eval)
```

## Design invariants (see CLAUDE.md)

- Deterministic layer (symbol graph + param/message tables) is ground truth; the LLM only
  synthesizes on top of verified facts.
- Eval-first: no retrieval/model/chunking change without a measured delta.
- AST-aware chunking only — never fixed-window splitting of C++.
- Mandatory citations; the retrieval-auditor removes anything it can't verify (fail closed).
- Domains map to ArduPilot's real architecture; the graph handles cross-domain questions.
- Fine-tuning (LoRA) is Phase 4, gated on measured retrieval gaps — not the default.

Subagent notes: subagents do not inherit the main session's skills (each declares its own in
frontmatter), and edits to agent files on disk require a session restart to take effect (edits
via `/agents` apply immediately).
