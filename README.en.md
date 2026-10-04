<div align="center">

<h1>AIALRA SourceLoom</h1>

<p><strong>Keep originals and turn model returns into readable, traceable document packages</strong></p>

<p>A material workbench · Local use or an authenticated single-user remote service</p>

<p><a href="README.md">简体中文</a> · <a href="docs/PROCESSOR_V11_USE.md">Usage guide</a> · <a href="deploy/README.md">Production deployment</a> · <a href="SECURITY.md">Security</a></p>

</div>

## 1 Current product

Import the complete original, prepare one model task package, receive Markdown, restore images and source locations, then read, edit, export, or hand off to ReadWeave

- Originals, model returns, and saved versions stay separate
- Nested folders, multiple selection, moves, archive, trash, and restore affect management metadata rather than document text
- Continuous source reading provides native text selection, search, zoom, and source-page references; paired panes scroll independently or follow each other
- Resource issues offer previews and concrete choices; mechanical checks do not replace semantic review
- Manual and automatic handoff share processing. New automatic generation only uses Web Chat ordinary chat mode and the user-designated Web-only business credential

Older multi-role generation code remains for historical task compatibility. It is not the default workflow for new materials, and the processor does not require the legacy Worker

<div align="center">

<img src="docs/assets/readme/processor-desktop.png" width="1120" alt="SourceLoom material tree and paired reader using synthetic content">

Figure 1.1 Current workbench using synthetic content; this demonstrates management and reading, not generated-content quality

</div>

## 2 Run locally

Use Python 3.11 or newer, from the repository root:

```bash
# Create an isolated environment
python -m venv .venv
```

- Windows: run `.venv/Scripts/Activate.ps1`
- macOS or Linux: run `source .venv/bin/activate`

```bash
# Install the product and pinned dependencies
python -m pip install .
# Start the loopback-only workbench
python -m sourceloom.cli serve --port 8765
```

Open <http://127.0.0.1:8765/>. No API key is needed for import, manual handoff, or local export. A clean installation includes no private materials

The default store is `data/`; private configuration is read from `.local/config.json`. Override with `SOURCELOOM_DATA` and `SOURCELOOM_CONFIG`. Stop the relevant service and back up before changing stores; do not run multiple writers against the same store

The official site uses the same processor through one web service behind the existing HTTPS and login proxy. Normal manual processing needs no generation Worker. See the [deployment guide](deploy/README.md) for authentication, required-material migration, public links, and rollback that preserves new writes. A repository commit and passing CI do not establish that an instance is deployed

## 3 Complete your first material

1. Import the original and inspect its files, extracted text, and resources
2. Download the complete task package and copy the start instruction. Upload to a model session with file tools, using your chosen depth
3. Import the returned Markdown through the normal result entry. The processor saves a new version and restores resources without hand-written image paths
4. Read the draft, compare source pages, and resolve necessary issues. See the [usage guide](docs/PROCESSOR_V11_USE.md)
5. Export a reading package or import into your explicitly configured ReadWeave destination. A reliable receipt allows opening the corresponding note

Receiving a ZIP does not prove that a model unpacked it or inspected its images. Full real-session xhigh single-package capability remains unverified; the depth name alone does not establish attachment support

The highest Web depth must use parameters confirmed by the live Web catalog and page, rather than Codex effort settings or an assumed Pro requirement. Unsupported attachments, long inputs, login, or depth block that branch; never substitute an engine, account, or key. Configure an explicit `processor_router` business route with `execution_channel=chatgpt_web` and ordinary `chat` mode. Generation credentials are not inherited from legacy provider configuration.

The [historical Codex experiments](docs/CODEX_FILE_RECOVERY.md) are retained for audit and channel-independent mechanisms only. Their 40 jobs are excluded from Web Chat acceptance; that campaign had zero Web jobs and provides no Web success rate.

## 4 State and data boundaries

- Candidate is a saved, readable draft; mechanical checks do not automatically make it semantically Verified
- Scanned pages remain available as source pages; missing native text does not imply OCR completion
- UNKNOWN preserves the original request identity and costs rather than allowing an automatic retry
- ReadWeave import and readback require external configuration; its initialization performance is separate from SourceLoom reading
- Deletion normally moves materials to trash. Permanent deletion needs separate confirmation and never deletes ReadWeave notes

Originals, drafts, databases, sessions, private configuration, and acceptance videos stay in the corresponding private runtime environment. The repository distributes code, pinned dependencies, synthetic tests, and necessary static assets. See [third-party notices](THIRD_PARTY_NOTICES.md)

## 5 Development and verification

```bash
# Install synthetic test dependencies
python -m pip install '.[test]'
# Run bounded deterministic tests without paid model calls
python scripts/verify.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for frontend checks. Browser acceptance uses isolated materials and does not insert test text into real drafts. Private originals and runtime records are not distributed with public tests

Engineering reports from execution agents follow [AGENTS.md](AGENTS.md) and the separate [reporting rules](docs/EXECUTOR_REPORTING_RULES.md). These rules do not alter content-model writing instructions

## 6 Maintenance and license

Use this repository for ordinary support and improvements. Follow [SECURITY.md](SECURITY.md) for credentials or private material, and [CONTRIBUTING.md](CONTRIBUTING.md) for contributions. The project [LICENSE](LICENSE) does not replace licenses of dependencies or original materials
