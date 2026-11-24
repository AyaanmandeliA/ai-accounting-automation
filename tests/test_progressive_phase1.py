import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from document_processing.template_extractor import extract_from_templates, extract_with_template, load_supplier_templates
from extraction import ai_extractor
from extraction.routing import ROUTE_CLAUDE_REQUIRED, ROUTE_NO_AI_TEMPLATE, route_document_group


def fake_template(minimum_confidence: float = 0.9) -> dict:
    return {
        "id": "fake_telecom",
        "enabled": True,
        "supplier_name": "Example Telecom",
        "supplier_name_patterns": [r"\bexample telecom\b"],
        "invoice_number_regex": r"Invoice\s*No\.?\s*[:#-]?\s*([A-Z0-9-]+)",
        "date_regex": r"Invoice\s*Date\s*[:#-]?\s*(\d{4}-\d{2}-\d{2})",
        "subtotal_regex": r"Subtotal\s*[:#-]?\s*AED\s*([0-9,.]+)",
        "vat_regex": r"VAT\s*[:#-]?\s*AED\s*([0-9,.]+)",
        "total_regex": r"^\s*Total\s*[:#-]?\s*AED\s*([0-9,.]+)",
        "trn_regex": r"TRN\s*[:#-]?\s*(\d{15})",
        "default_account": "Telephone & Internet",
        "default_posting_type": "Bill",
        "default_vat_treatment": "Claimable VAT",
        "default_accounting_treatment": "supplier_bill_unpaid",
        "default_payment_method": "unpaid",
        "currency": "AED",
        "confidence_rules": {
            "minimum_confidence": minimum_confidence,
            "required_fields": ["supplier_name", "invoice_number", "invoice_date", "total_amount"],
            "field_weights": {
                "supplier_name": 0.2,
                "invoice_number": 0.2,
                "invoice_date": 0.2,
                "total_amount": 0.2,
                "tax_amount": 0.1,
                "supplier_trn": 0.1,
            },
        },
    }


def invoice_text() -> str:
    return (
        "Example Telecom Tax Invoice\n"
        "TRN: 123456789012345\n"
        "Invoice No: INV-100\n"
        "Invoice Date: 2026-06-01\n"
        "Subtotal: AED 100.00\n"
        "VAT: AED 5.00\n"
        "Total: AED 105.00\n"
    )


def configured_template(template_id: str) -> dict:
    for template in load_supplier_templates():
        if template.get("id") == template_id:
            return template
    raise AssertionError(f"Missing configured template {template_id}")


def example_chillers_remittance_text() -> str:
    return (
        "Remittance Advice\n"
        "Payment To Date: 20-04-2026\n"
        "Example Chillers LLC\n"
        "Bill Number Bill Date Due Date Original Amount Balance Payment\n"
        "EXC-0001 01-03-2026 15-03-2026 500.00 500.00 500.00\n"
        "EXC-0002 01-04-2026 15-04-2026 800.00 300.00 300.00\n"
        "Total: AED 800.00\n"
    )


class ProgressivePhase1Tests(unittest.TestCase):
    def _patch_dirs(self, base: Path):
        return patch.multiple(
            ai_extractor,
            RAW_DIR=base / "data/raw",
            PARSED_DIR=base / "data/parsed",
            EXTRACTED_DIR=base / "data/extracted",
            PROCESSED_DIR=base / "data/processed",
            PROCESSED_FILE=base / "data/processed/ai_extracted.json",
        )

    def test_template_extracts_invoice_fields(self):
        result = extract_with_template(invoice_text(), fake_template(), source_file="invoice.pdf")
        self.assertEqual(result["supplier_name"], "Example Telecom")
        self.assertEqual(result["invoice_number"], "INV-100")
        self.assertEqual(result["invoice_date"], "2026-06-01")
        self.assertEqual(result["total_amount"], 105.0)
        self.assertEqual(result["tax_amount"], 5.0)
        self.assertEqual(result["supplier_trn"], "123456789012345")
        self.assertEqual(result["line_items"][0]["account_name"], "Telephone & Internet")
        self.assertEqual(result["extraction_method"], "supplier_template")

    def test_configured_supplier_templates_extract_fake_local_text(self):
        cases = [
            (
                "adatum_international_invoice",
                "adatum.pdf",
                "Adatum International Co.LLC\nTRN: 100123456700003\n"
                "Tax Invoice ADT-1001\nInvoice Date: 01/06/2026\n"
                "Subtotal AED 1,000.00\nVAT AED 50.00\nTotal AED 1,050.00\n",
                "Adatum International Co.LLC",
                "ADT-1001",
                1050.0,
            ),
            (
                "fabrikam_trading_invoice",
                "fabrikam.pdf",
                "Fabrikam Trading Co. L.L.C.\nTRN: 100123456700004\n"
                "Tax Invoice FAB-2001\nInvoice Date: 02/06/2026\n"
                "Subtotal AED 2,000.00\nVAT AED 100.00\nTotal AED 2,100.00\n",
                "Fabrikam Trading Co. L.L.C.",
                "FAB-2001",
                2100.0,
            ),
            (
                "litware_hair_invoice",
                "litware.pdf",
                "Litware General Trading LLC\nHair Products\nTRN: 100123456700005\n"
                "Tax Invoice LIT-INV-3001\nInvoice Date: 03/06/2026\n"
                "Subtotal AED 3,000.00\nVAT AED 150.00\nTotal AED 3,150.00\n",
                "Litware General Trading LLC",
                "LIT-INV-3001",
                3150.0,
            ),
            (
                "contoso_receipt",
                "contoso.jpeg",
                "Contoso Tax Invoice\nTRN: 100123456700006\n"
                "Receipt No: 400001\nDate: 04/06/2026\n"
                "Subtotal AED 400.00\nVAT AED 20.00\nTotal AED 420.00\n",
                "Contoso",
                "400001",
                420.0,
            ),
            (
                "example_hypermarket_receipt",
                "example-hypermarket.jpeg",
                "Example Hypermarket\nTRN: 100123456700007\n"
                "Bill No: B-5001\nDate: 05/06/2026\n"
                "Subtotal AED 50.00\nVAT AED 2.50\nTotal AED 52.50\n",
                "Example Hypermarket",
                "B-5001",
                52.5,
            ),
            (
                "example_fuel_fuel_receipt",
                "fuel.jpeg",
                "EXAMPLEFUEL Fuel Receipt\nTRN: 100123456700008\n"
                "Receipt No: F-6001\nDate: 06/06/2026\n"
                "Subtotal AED 60.00\nVAT AED 3.00\nTotal AED 63.00\n",
                "Examplefuel",
                "F-6001",
                63.0,
            ),
        ]
        for template_id, filename, text, supplier, invoice_number, total in cases:
            with self.subTest(template_id=template_id):
                result = extract_from_templates(
                    text,
                    source_file=filename,
                    templates=[configured_template(template_id)],
                )
                self.assertIsNotNone(result)
                self.assertEqual(result["supplier_name"], supplier)
                self.assertEqual(result["invoice_number"], invoice_number)
                self.assertEqual(result["total_amount"], total)
                self.assertEqual(result["extraction_method"], "supplier_template")
                self.assertGreaterEqual(result["template_confidence"], 0.9)

    def test_northwind_payment_slip_routes_supporting_only_no_ai(self):
        text = (
            "Northwind Mail - Re: Payment Slip - January 2030\n"
            "services, billing <billing@example.com>\n"
            "Please find payment slip correspondence attached.\n"
        )
        decision = route_document_group(
            [Path("Northwind Mail - Re_ Payment Slip - January 2030.pdf")],
            text_preview={"text": text, "quality_score": 0.95},
            templates=[configured_template("northwind_payment_slip_email_supporting")],
        )
        self.assertEqual(decision.route, ROUTE_NO_AI_TEMPLATE)
        self.assertEqual(decision.extracted["document_type"], "supporting_only")
        self.assertEqual(decision.extracted["qbo_posting_type"], "None")
        self.assertIsNone(decision.extracted["total_amount"])

    def test_example_chillers_remittance_template_extracts_multiple_bill_rows(self):
        result = extract_from_templates(
            example_chillers_remittance_text(),
            source_file="Example Chillers Mar Apr.pdf",
            templates=[configured_template("example_chillers_chiller_remittance_table")],
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["template_id"], "example_chillers_chiller_remittance_table")
        self.assertEqual(len(result["transactions"]), 2)
        first, second = result["transactions"]
        self.assertEqual(first["invoice_number"], "EXC-0001")
        self.assertEqual(first["invoice_date"], "2026-03-01")
        self.assertEqual(first["due_date"], "2026-03-15")
        self.assertEqual(first["total_amount"], 500.0)
        self.assertEqual(first["payment_amount"], 500.0)
        self.assertTrue(first["payment_confirmed"])
        self.assertEqual(first["payment_date"], "2026-04-20")
        self.assertEqual(first["line_items"][0]["account_name"], "Utilities")
        self.assertEqual(second["invoice_number"], "EXC-0002")
        self.assertEqual(second["total_amount"], 800.0)
        self.assertEqual(second["payment_amount"], 300.0)

    def test_known_supplier_routes_to_no_ai(self):
        path = Path("invoice.pdf")
        decision = route_document_group(
            [path],
            text_preview={"text": invoice_text(), "quality_score": 0.95},
            templates=[fake_template()],
        )
        self.assertEqual(decision.route, ROUTE_NO_AI_TEMPLATE)
        self.assertEqual(decision.template_id, "fake_telecom")
        self.assertGreaterEqual(decision.confidence, 0.9)

    def test_low_confidence_template_routes_to_claude(self):
        weak_text = "Example Telecom Invoice No: INV-100 Total: AED 105.00"
        decision = route_document_group(
            [Path("invoice.pdf")],
            text_preview={"text": weak_text, "quality_score": 0.95},
            templates=[fake_template()],
        )
        self.assertEqual(decision.route, ROUTE_CLAUDE_REQUIRED)
        self.assertLess(decision.confidence, 0.9)

    def test_dry_run_shows_no_ai_savings(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            invoice = raw / "invoice.pdf"
            invoice.write_text("placeholder")
            stdout = StringIO()
            preview = {
                "text": invoice_text(),
                "quality_score": 0.95,
                "page_count": 1,
                "method": "text_extract_ok",
                "warnings": [],
            }
            with self._patch_dirs(base), \
                 patch("extraction.ai_extractor.extract_text_preview", return_value=preview), \
                 patch("extraction.ai_extractor.load_supplier_templates", return_value=[fake_template()]), \
                 patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client, \
                 redirect_stdout(stdout):
                result = ai_extractor.run(dry_run=True)

        output = stdout.getvalue()
        self.assertEqual(result, [])
        anthropic_client.assert_not_called()
        self.assertIn("no-AI document count: 1", output)
        self.assertIn("Claude-required document count: 0", output)
        self.assertIn("suppliers matched by template: Example Telecom", output)
        self.assertIn("no_ai_template=1", output)

    def test_template_route_does_not_call_claude_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            invoice = raw / "invoice.pdf"
            invoice.write_text("placeholder")
            preview = {
                "text": invoice_text(),
                "quality_score": 0.95,
                "page_count": 1,
                "method": "text_extract_ok",
                "warnings": [],
            }
            with self._patch_dirs(base), \
                 patch("extraction.ai_extractor.extract_text_preview", return_value=preview), \
                 patch("extraction.ai_extractor.load_supplier_templates", return_value=[fake_template()]), \
                 patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("allocation.review_router.REVIEW_ROOT", base / "data/review"), \
                 patch("extraction.ai_extractor._load_api_key") as load_api_key, \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                results = ai_extractor.run(dry_run=False, assume_yes=True)

            self.assertEqual(len(results), 1)
            load_api_key.assert_not_called()
            anthropic_client.assert_not_called()
            self.assertEqual(results[0]["extraction_method"], "supplier_template")
            self.assertEqual(results[0]["source_file"], str(invoice.resolve()))
            self.assertEqual(results[0]["all_source_files"], [str(invoice.resolve())])
            self.assertTrue(Path(results[0]["extracted_file"]).exists())

    def test_template_route_can_save_multiple_transactions_without_claude(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = base / "data/raw"
            raw.mkdir(parents=True)
            invoice = raw / "0000000000000000_Example Chillers Mar_ Apr.pdf"
            invoice.write_text("placeholder")
            preview = {
                "text": example_chillers_remittance_text(),
                "quality_score": 0.95,
                "page_count": 1,
                "method": "text_extract_ok",
                "warnings": [],
            }
            with self._patch_dirs(base), \
                 patch("extraction.ai_extractor.extract_text_preview", return_value=preview), \
                 patch("extraction.ai_extractor.load_supplier_templates", return_value=[configured_template("example_chillers_chiller_remittance_table")]), \
                 patch("extraction.ai_extractor.load_coa", return_value={}), \
                 patch("extraction.ai_extractor.load_suppliers", return_value={}), \
                 patch("extraction.ai_extractor.load_supplier_account_map", return_value={}), \
                 patch("allocation.review_router.REVIEW_ROOT", base / "data/review"), \
                 patch("extraction.ai_extractor._load_api_key") as load_api_key, \
                 patch("extraction.ai_extractor.anthropic.Anthropic") as anthropic_client:
                results = ai_extractor.run(dry_run=False, assume_yes=True)

            self.assertEqual(len(results), 2)
            load_api_key.assert_not_called()
            anthropic_client.assert_not_called()
            self.assertEqual(
                {result["invoice_number"] for result in results},
                {"EXC-0001", "EXC-0002"},
            )
            for result in results:
                self.assertEqual(result["extraction_method"], "supplier_template")
                self.assertEqual(result["source_file"], str(invoice.resolve()))
                self.assertTrue(Path(result["extracted_file"]).exists())


if __name__ == "__main__":
    unittest.main()
