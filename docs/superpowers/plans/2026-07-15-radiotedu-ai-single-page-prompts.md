# RadioTEDU `/ai` Single-Page Prompts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make both target-machine starter prompts unambiguously assign the only listener UI to `https://radiotedu.com/ai` and reserve `https://stream.radiotedu.com/en` and `/fr` exclusively for Icecast audio.

**Architecture:** Keep the builder repository's two-machine handoff boundary: the broadcast computer owns autonomous playout and Icecast source connections, while the website computer owns the read-only `/ai` listener experience. Encode the approved routing and Andon FM-inspired visual direction in contract tests first, then update both prompts and both runbooks to satisfy the same wording.

**Tech Stack:** Markdown handoff prompts and runbooks, Python `pytest` packaging-contract tests, Git.

## Global Constraints

- The only listener UI is `https://radiotedu.com/ai`.
- English and French are selected inside the single `/ai` page; do not create `/ai/en` or `/ai/fr` listener pages.
- `https://stream.radiotedu.com` is Icecast-only and exposes audio mounts `/en` and `/fr`; it does not host HTML, APIs, or application routes.
- The `/ai` page uses an Andon FM-inspired dark, minimal, atmospheric hierarchy with original RadioTEDU branding and assets.
- Browser-local play/pause never controls broadcast playout.
- Do not change the Icecast host, port, AAC-LC 192 kbps profile, source authentication, or station isolation contract.
- Stop after staging and conformance verification; do not deploy to production.

---

### Task 1: Lock the target-machine routing contract with a failing test

**Files:**
- Modify: `tests/backend/test_desktop_packaging.py:202`
- Test: `tests/backend/test_desktop_packaging.py`

**Interfaces:**
- Consumes: UTF-8 Markdown from `handoff/web-server/prompt.md` and `handoff/broadcast-server/prompt.md`.
- Produces: A packaging test that requires the single-page listener, Icecast-only stream origin, in-page station selector, and approved visual direction in both handoff prompts.

- [ ] **Step 1: Extend the prompt contract test**

Add these assertions to `test_web_prompt_remains_status_only_and_two_prompts_are_canonical` after the existing no-control assertions:

```python
    broadcast_prompt = (ROOT / "handoff" / "broadcast-server" / "prompt.md").read_text(encoding="utf-8").casefold()
    for prompt in (web_prompt, broadcast_prompt):
        assert "https://radiotedu.com/ai" in prompt
        assert "icecast-only" in prompt
        assert "https://stream.radiotedu.com/en" in prompt
        assert "https://stream.radiotedu.com/fr" in prompt
        assert "do not create `/ai/en` or `/ai/fr`" in prompt

    assert "single listener page" in web_prompt
    assert "in-page en/fr station selector" in web_prompt
    assert "andon fm-inspired" in web_prompt
    assert "original radiotedu" in web_prompt
    assert "must not serve html, api, or application routes" in web_prompt
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m pytest tests/backend/test_desktop_packaging.py::test_web_prompt_remains_status_only_and_two_prompts_are_canonical -q
```

Expected: FAIL because the current prompts still describe `/ai/en` and `/ai/fr` listener routes and do not contain the new single-page, Icecast-only, or Andon FM-inspired contract phrases.

### Task 2: Update the two starter prompts and operational runbooks

**Files:**
- Modify: `handoff/web-server/prompt.md`
- Modify: `handoff/broadcast-server/prompt.md`
- Modify: `docs/WEBSITE_SERVER_RUNBOOK.md`
- Modify: `docs/BROADCAST_COMPUTER_RUNBOOK.md`
- Test: `tests/backend/test_desktop_packaging.py`

**Interfaces:**
- Consumes: The assertions introduced in Task 1 and the approved design in `docs/superpowers/specs/2026-07-15-radiotedu-ai-andon-style-design.md`.
- Produces: Two standalone Codex starter prompts and matching runbooks with one consistent domain/route contract.

- [ ] **Step 1: Update the web-server starter prompt**

Replace the language-specific listener route contract with this exact responsibility statement:

```markdown
- Listener page: `https://radiotedu.com/ai`; this is the single listener page.
- Language/station choice: one visible, keyboard-accessible in-page EN/FR station selector; do not create `/ai/en` or `/ai/fr`.
- Icecast-only origin: `https://stream.radiotedu.com`, with public audio mounts `/en` and `/fr`. It must not serve HTML, API, or application routes.
```

Replace the restrictive Andon sentence with:

```markdown
Use an Andon FM-inspired dark, minimal, atmospheric information hierarchy: prominent central listening control, strong now-playing focus, restrained typography, generous spacing, and calm supporting information. Keep original RadioTEDU branding, copy, colors, and assets; do not copy Andon FM logos, text, illustrations, source assets, or distinctive branded artwork.
```

Update staging checks so they verify only `https://radiotedu.com/ai`, its in-page EN/FR selector, and the two Icecast audio sources.

- [ ] **Step 2: Update the broadcast-computer starter prompt**

State that the broadcast computer publishes audio to the `/en` and `/fr` Icecast mounts, reports their public URLs, and never hosts the listener UI. Add this exact boundary:

```markdown
- Listener page: `https://radiotedu.com/ai`; this is the single listener page and the broadcast computer does not host it. Do not create `/ai/en` or `/ai/fr`.
- Icecast-only origin: `https://stream.radiotedu.com`, exposing only the public audio mounts `/en` and `/fr`; it does not host the listener UI or API.
```

- [ ] **Step 3: Align both operational runbooks**

In `docs/WEBSITE_SERVER_RUNBOOK.md`, replace the three listener-route bullets with the single absolute `/ai` URL and explain the in-page station selector. Describe `stream.radiotedu.com` as Icecast-only and add the approved Andon FM-inspired visual direction.

In `docs/BROADCAST_COMPUTER_RUNBOOK.md`, add the same absolute listener URL and Icecast-only host boundary beside the fixed mount contract.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run:

```powershell
python -m pytest tests/backend/test_desktop_packaging.py::test_web_prompt_remains_status_only_and_two_prompts_are_canonical -q
```

Expected: `1 passed`.

- [ ] **Step 5: Run the complete packaging-contract test file**

Run:

```powershell
python -m pytest tests/backend/test_desktop_packaging.py -q
```

Expected: all tests in the file pass. If older assertions intentionally inspect the current builder implementation's `/ai/en` and `/ai/fr` compatibility routes, leave those implementation checks unchanged; this task changes the target-machine handoff contract, not the builder application's runtime routes.

### Task 3: Verify, commit, and publish the starter prompts

**Files:**
- Verify: `handoff/web-server/prompt.md`
- Verify: `handoff/broadcast-server/prompt.md`
- Verify: `docs/WEBSITE_SERVER_RUNBOOK.md`
- Verify: `docs/BROADCAST_COMPUTER_RUNBOOK.md`
- Verify: `tests/backend/test_desktop_packaging.py`

**Interfaces:**
- Consumes: The completed prompt, runbook, and test changes from Tasks 1–2.
- Produces: A clean feature-branch commit pushed to `origin/feature/dual-station-radiotedu` and two copy-ready starter prompts.

- [ ] **Step 1: Run static consistency checks**

Run:

```powershell
git diff --check
rg -n "radiotedu\.com/ai|stream\.radiotedu\.com|/ai/en|/ai/fr|Andon FM" handoff docs/WEBSITE_SERVER_RUNBOOK.md docs/BROADCAST_COMPUTER_RUNBOOK.md
```

Expected: no whitespace errors; every target-machine document assigns UI to `radiotedu.com/ai`, treats `/ai/en` and `/ai/fr` only as prohibited listener routes, and uses `/en` and `/fr` only as Icecast mounts.

- [ ] **Step 2: Review the staged diff and secret scan**

Run:

```powershell
git diff --stat
rg -n "<icecast-source-password>" handoff docs tests/backend/test_desktop_packaging.py
```

Expected: only planned files are changed and the credential scan returns no matches.

- [ ] **Step 3: Commit the implementation**

Run:

```powershell
git add tests/backend/test_desktop_packaging.py handoff/web-server/prompt.md handoff/broadcast-server/prompt.md docs/WEBSITE_SERVER_RUNBOOK.md docs/BROADCAST_COMPUTER_RUNBOOK.md docs/superpowers/plans/2026-07-15-radiotedu-ai-single-page-prompts.md
git commit -m "docs: clarify RadioTEDU listener and stream domains"
```

- [ ] **Step 4: Push the feature branch**

Run:

```powershell
git push origin feature/dual-station-radiotedu
```

Expected: the remote feature branch advances to the new commit without changing `main`.
