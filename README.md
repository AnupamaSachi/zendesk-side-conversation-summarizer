# Zendesk Side-Conversation Summarizer

This repository contains a sanitized template for a local workflow that reads
qualifying Zendesk tickets, extracts side-conversation content, asks a local
Codex CLI for a title and description, and writes JSON/CSV output.

The repository intentionally contains no real ticket exports, customer data,
company credentials, internal identifiers, or production URLs. Configure those
values locally through `.env` and `config/products.json`.

## Local setup

1. Copy `.env.example` to `.env`.
2. Add approved Zendesk and read-only Jira credentials locally.
3. Replace the example product mappings in `config/products.json`.
4. Run a synthetic test:

   ```bash
   python3 src/local_tracker.py --sample --limit 1
   ```

5. Run a real export only after access and data-handling approval:

   ```bash
   python3 src/local_tracker.py --start-date YYYY-MM-DD --limit 500
   ```

Generated CSV and JSON files are ignored by Git and should remain local.

## Data handling

The workflow should use the minimum required Zendesk fields, send only approved
side-conversation content to the approved local summarization tool, keep Jira
access read-only, and write to an approved destination with restricted access.
Never commit API tokens, OAuth secrets, customer data, or production exports.
