# Accounting automation pilot

A Python tool that turns supplier documents (invoices, receipts, statements, payment slips) into draft QuickBooks Online entries, with a human review step before anything is posted.

## What it does

1. Ingests documents from Gmail attachments and Google Drive folders, or from local files.
2. Extracts supplier, invoice number, dates, amounts, VAT, TRN and line items. Known suppliers can be read with local regex templates; other documents go to a document-AI provider or to Claude, depending on configuration and cost limits.
3. Applies accounting rules: document type, VAT treatment (UAE 5%), paid or unpaid, bill vs expense vs supplier credit, and brand/branch allocation from folder paths and vendor rules.
4. Routes anything uncertain to review. Reviewers use a local command-line review or a localhost-only web dashboard, and corrections are saved as rules for later documents.
5. Builds QuickBooks objects (Bill, Expense, SupplierCredit, BillPayment). Live posting needs explicit flags (`--post --confirm-live-posting`); `--dry-run` plans without calling live services.

## How it works

- `ingestion/` Gmail and Drive ingestion, spreadsheet parsing, source metadata.
- `document_processing/` text extraction, media optimisation, supplier templates, provider output normalisation.
- `extraction/` routing between template, provider and Claude extraction; chart-of-accounts and supplier memory retrieval; model routing.
- `accounting/` decision engine, VAT and posting rules, payment matching, reconciliation frameworks, cost controls, audit log, SQLite persistence.
- `allocation/` brand, branch and QuickBooks class resolution; review routing.
- `qbo/` QuickBooks OAuth and posting, plus dry-run builders.
- `review.py`, `review_dashboard.py`, `review_dashboard_static/` review tools.
- `benchmarks/` scripts for comparing document-AI providers.
- `main.py` command-line entry point (`python main.py --help`).

Secrets are loaded from files outside the repository (paths set by the variables in `.env.example`); see `secrets_manager.py`.

## Stack

Python, Anthropic Claude API, Google Gmail and Drive APIs, Google Document AI (optional), QuickBooks Online API (`python-quickbooks`, `intuitlib`), pandas, openpyxl, Pillow.

## Tests

`python -m unittest discover -s tests`. The tests use synthetic data and mocks and do not call live services.

## Status

Paused sandbox experiment; accuracy and economics unproven.

## Removed for privacy

This is a cleaned snapshot without history. Not included: credentials and tokens, real invoices and receipts, extracted data, ledgers and accounting exports, supplier lists, the real chart of accounts, run logs, benchmark results and internal notes. Config files with business data were replaced with small synthetic samples (made-up brands, branches and suppliers), and supplier names, invoice numbers, amounts and cloud project identifiers in tests were replaced with synthetic values.

I set the requirements and check the results. AI tools write most of the code.
