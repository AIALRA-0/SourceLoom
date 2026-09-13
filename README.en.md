<div align="center">

<h1>AIALRA SourceLoom</h1>

<p><strong>Weave source material into traceable learning content</strong></p>

<p>Content preparation for ReadWeave · Personal preview 0.1</p>

[简体中文](README.md) · [Workspace (login required)](https://sourceloom.aialra.online) · [Master plan](docs/MASTER_PLAN.md) · [Verification report](reports/IMPLEMENTATION_STATUS.md)

</div>

## 1 Prepare material for learning

SourceLoom preserves original files, inventories information before generation, and gives planning, writing, review, and local repair separate tasks. It produces candidate packages for [ReadWeave](https://github.com/AIALRA-0/ReadWeave), where reading and further questions continue.

Its initial scope is short documents, selected chapters, and papers.

<div align="center">

<img src="docs/assets/readme/workspace-desktop.png" width="1120" alt="The running SourceLoom workspace with upload controls, a three-step explanation, and an original synthetic example">

Figure 1.1 Actual local workspace using synthetic material only

</div>

## 2 Current capabilities

- Preserve original bytes and inventory text, images, tables, formula source, code, links, and footnotes
- Freeze preservation obligations before generation and check source quotes, coverage mappings, and protected objects
- Execute independent inventory, planning, generation, review, repair, and research-planning roles
- Apply exact-text patches against a specific revision, with a two-round repair limit
- Use manual task packages, local Codex, compatible APIs, or an existing durable task router
- Enforce per-document reservations, daily totals, call limits, and uncertainty without automatic replay
- Export native ReadWeave packages, add candidates under a configured parent, and read back content and attachments

<div align="center">

<img src="docs/assets/readme/omission-check.png" width="1000" alt="A removed condition paragraph produces an actual missing-obligation finding">

Figure 2.1 The running omission check after removing a condition paragraph

</div>

## 3 First result in five minutes

Use Python 3.11 or later. Download this repository and open a terminal in its root directory, then use an isolated environment.

Create a virtual environment.

```bash
python -m venv .venv
```

Activate it with `.venv\Scripts\Activate.ps1` on Windows or `source .venv/bin/activate` on other systems. Then install and start the project.

```bash
python -m pip install .
python -m sourceloom.cli serve
```

Open the local address printed by the command; the default port is `8765`.

- Select the free synthetic demo
- Open the coverage and repair view, remove one condition paragraph, and restore it
- Download the native candidate package from the export view
- Import it under a test parent in your own ReadWeave instance

The synthetic demo makes no model calls. It demonstrates behavior and structural checks, not model quality.

## 4 Models and writing policy

Copy [the configuration example](config.example.json) to a private location outside Git. Set `SOURCELOOM_CONFIG` to that file and `SOURCELOOM_DATA` to your material directory.

Real planning, writing, and review require the full [human-readable Chinese technical writing skill](https://github.com/AIALRA-0/agent-human-readable-technical-writing). Set `writing_skill_dir` to its local directory.

The skill owns writing standards; SourceLoom owns storage, roles, budgets, verification, and import. Task artifacts identify the policy snapshot used.

Start with a short document and the default limits: USD 0.50 per document, USD 2 per day, and 80 daily calls. DeepSeek thinking behavior should be explicitly configured. See [models and budgets](docs/MODELS_AND_BUDGET.md).

## 5 Evidence and limitations

- 101 deterministic tests, including five format variants of twelve independent synthetic domain seeds
- 28 browser checks across desktop, tablet, mobile, and light/dark views
- Native import and actual ReadWeave readback, including repeated images, merged cells, code, and footnote targets
- Real Sol and DeepSeek calls with measured usage, recorded failures, and unresolved review results

```bash
python -m pip install ".[test]"
python scripts/verify.py
```

The verification runner has a 120-second timeout. See [implementation status](reports/IMPLEMENTATION_STATUS.md) for the exact scope.

Preserved bytes, structural coverage, semantic fidelity, import stability, and user acceptance are separate claims.

PDF and DOCX extraction retain explicit unresolved items. Automated visual review, complex formula layout, broad real-document quality evaluation, and ReadWeave editor save/reopen verification remain incomplete. Research mode currently provides question planning and specified-source intake.

## 6 Documentation and deployment

- [Master plan](docs/MASTER_PLAN.md)
- [Requirement ledger](docs/REQUIREMENTS.md)
- [Invariants and proof boundaries](docs/INVARIANTS.md)
- [Reading preferences and examples](docs/READING_PREFERENCES.md)
- [Models, resources, and budgets](docs/MODELS_AND_BUDGET.md)
- [Lessons from existing projects](docs/PROJECT_LESSONS.md)
- [Deployment and rollback](deploy/README.md)

Production uses an existing authenticated gateway. Public source excludes private attachments, credentials, production configuration, databases, and login records.

## 7 Contributing and security

Use synthetic reproduction material where possible. See [contribution guidance](CONTRIBUTING.md) and [security information](SECURITY.md). Never post credentials or private source documents in public issues.

Licensed under [Apache-2.0](LICENSE). See [third-party notices](THIRD_PARTY_NOTICES.md) for dependencies and integrations.
