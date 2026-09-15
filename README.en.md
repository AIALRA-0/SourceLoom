<div align="center">

<h1>AIALRA SourceLoom</h1>

<p><strong>Turn source material into readable, traceable content</strong></p>

<p>Content preparation for ReadWeave · Automatic production under evaluation</p>

[中文](README.md) · [Actual trials](reports/AUTOMATIC_TRIAL.md) · [Master plan](docs/MASTER_PLAN.md) · [Deployment](deploy/README.md)

</div>

## 1 Current results

SourceLoom preserves originals and separates inventory, planning, writing, review, and local repair before preparing material for ReadWeave. Default rewriting keeps the source purpose and improves clarity and transitions, without inventing classroom scenarios, exercises, or extensions

Reading performance, the toolbar, bidirectional navigation and the editor have been updated. Actual papers, webpages, documents and plain text exercise background intake and saved outputs. There are still **zero formally accepted automatic outputs**

DeepSeek has produced readable drafts, and one independent chat comparison of the complete original and candidate has been recovered successfully. Writing and fidelity requirements remain unmet. See [this iteration's real inputs and results](docs/ITERATION_8.md), [earlier performance measurements](docs/ITERATION_6.md), and [workbench updates](docs/ITERATION_4.md)

Earlier manually revised examples remain [historical examples](docs/REAL_CASES.md), not evidence of unattended automatic quality

<div align="center">

<img src="docs/assets/readme/library-desktop.png" width="1120" alt="Actual SourceLoom document tree and source comparison using synthetic material only">

Figure 1.1 The actual library interface, using synthetic material to demonstrate document management and object rendering

</div>

## 2 Using the library

1. Select files or enter a webpage address; file uploads display actual transfer progress
2. Choose a folder and wait for confirmation that the server has saved the input before closing the page
3. With a configured provider, request rewriting during import or start it later; the server keeps processing and the document shows the current stage and part
4. Reopen the material to compare its source and available output; click linked text on either side to locate the other side, choosing among multiple sources when needed. Stopped jobs keep any drafts already produced
5. Download the text or send an eligible candidate archive to ReadWeave

Rename, move, duplicate, trash, and restore documents. Editing saves a new version and invalidates previous acceptance

The tree supports context menus, keyboard navigation, multi-selection and nested-folder restoration. The editor supports highlighting, line numbers, find and replace, undo, preview, autosave and conflict recovery. Heading numbers can be preserved, shown or hidden. See [rewriting and workbench changes](docs/ITERATION_4.md)

See [the previous iteration](docs/ITERATION_3.md) for long-source preflight, source navigation, and measured performance

## 3 Run locally

Requires Python 3.11 or later

```bash
# Create an isolated environment
python -m venv .venv
```

Activate it with `.venv/Scripts/Activate.ps1` on Windows or `source .venv/bin/activate` elsewhere

```bash
# Install the application
python -m pip install .

# Start the local library, listening on port 8765 by default
python -m sourceloom.cli serve
```

Open the local address printed by the server. In another terminal with the same configuration and data directory:

```bash
# Process persisted jobs independently of the browser
python -m sourceloom.cli worker
```

Without a configured provider, the library stores and manages material but explains why generation is unavailable

## 4 Full writing skill and providers

Copy [the example configuration](config.example.json) to a private location. Set `SOURCELOOM_CONFIG` to that file and `SOURCELOOM_DATA` to the material directory

Point `writing_skill_dir` at the complete [Chinese technical writing skill](https://github.com/AIALRA-0/agent-human-readable-technical-writing). Every role receives the unabridged effective instruction files, while the entire package is frozen alongside the job. See [delivery details](docs/SKILL_RUNTIME.md)

Receiving instructions does not prove compliance. Formal output requires teaching review, writing review, and independent source comparison of the same draft revision. Unreviewed output remains a draft

Paid API requests default to at most 24 per document, excluding subscription requests, with two prose repair rounds per document. These are workflow settings, not the user's subscription allowance. There is no default document or project elapsed-time limit; individual network requests remain bounded. The default document budget is $0.20

Cumulative subscription calls have no default limit. Generation and repair prioritize DeepSeek. Complex-material experiments record their adjusted per-document cash protection and cumulative spending separately. Trial limits and unknown subscription costs are recorded in [the trial report](reports/AUTOMATIC_TRIAL.md)

## 5 Verified scope and limits

- Deterministic tests cover folders, document management, versions, trash, and persistent job recovery
- Web intake stores original HTML and bounded raster downloads; missing assets remain explicit gaps, and dynamic pages are not comprehensively supported
- Ordinary word-processing documents expose text, tables, links, and media; complex equations, tracked changes, and unsupported objects remain explicit gaps
- Real browser upload checks cover reopening after closing the page, byte-exact original downloads, available draft downloads and stage progress; malformed model responses are not marked as completed generation
- One synthetic mixed document completed real ReadWeave import, editing, saving, and reopening with repeated images, tables, code, math, paragraph anchors, and footnote targets preserved
- The 24-document real and held-out evaluation is incomplete, and a $0.10 average full-pipeline cost has not been demonstrated

```bash
# Install test dependencies
python -m pip install ".[test]"

# Run deterministic checks with a 120-second deadline and no paid model calls
python scripts/verify.py
```

Passing program tests does not establish writing quality or user acceptance

## 6 Further reading

- [Master plan](docs/MASTER_PLAN.md)
- [Requirements](docs/REQUIREMENTS.md)
- [Full skill delivery](docs/SKILL_RUNTIME.md)
- [Actual trial results](reports/AUTOMATIC_TRIAL.md)
- [Project lessons](docs/PROJECT_LESSONS.md)
- [Deployment and rollback](deploy/README.md)
- [Contributing](CONTRIBUTING.md)
- [Security](SECURITY.md)

Private documents, provider credentials, production configuration, login records, and raw model requests are excluded from the public repository
