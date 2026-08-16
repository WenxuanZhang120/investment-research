import hashlib
import json
import os
import re
import tempfile
import unittest
from pathlib import Path


from scripts.validate_public_site import validate_public_site


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PAGES_WORKFLOW = REPOSITORY_ROOT / ".github/workflows/validate.yml"


class ValidatePublicSiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "dist"
        (self.root / "assets").mkdir(parents=True)
        (self.root / "index.html").write_text(
            """<!doctype html><html><head>
            <meta http-equiv="Content-Security-Policy" content="default-src 'self'; base-uri 'self'; object-src 'none'; form-action 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'">
            <meta name="referrer" content="no-referrer">
            </head><body></body></html>""",
            encoding="utf-8",
        )
        (self.root / "og.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (self.root / "assets/app.js").write_text("export {};", encoding="utf-8")
        (self.root / "assets/app.css").write_text("body{}", encoding="utf-8")
        self.write_minimal_payloads()
        self.write_manifest()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_json(self, relative: str, value) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return path

    def write_minimal_payloads(self) -> None:
        self.write_json(
            "data/status/latest.json",
            {
                "schemaVersion": 1,
                "status": "missing",
                "artifactAvailable": False,
                "run": None,
            },
        )
        shards = []
        for shard in "0123456789abcdef":
            market_path = f"market/queue-{shard}.json"
            shards.append({"name": shard, "path": market_path, "recordCount": 0})
            self.write_json(
                f"data/{market_path}",
                {
                    "schemaVersion": 1,
                    "status": "missing",
                    "shard": shard,
                    "recordCount": 0,
                    "records": [],
                },
            )
            self.write_json(
                f"data/companies/details-{shard}.json",
                {
                    "schemaVersion": 1,
                    "status": "missing",
                    "shard": shard,
                    "recordCount": 0,
                    "companies": [],
                },
            )
        self.write_json(
            "data/market/summary.json",
            {
                "schemaVersion": 1,
                "status": "missing",
                "asOfDate": None,
                "bundleId": None,
                "screeningVersion": None,
                "purpose": None,
                "recordCount": 0,
                "eligibleCount": 0,
                "rejectCount": 0,
                "priorityCounts": {},
                "shards": shards,
            },
        )
        self.write_json(
            "data/companies/index.json",
            {
                "schemaVersion": 1,
                "status": "missing",
                "recordCount": 0,
                "companies": [],
            },
        )
        self.write_json(
            "data/etf/index.json",
            {
                "schemaVersion": 1,
                "status": "missing",
                "asOfDate": None,
                "recordCount": 0,
                "records": [],
            },
        )
        self.write_json(
            "data/content/index.json",
            {
                "schemaVersion": 1,
                "status": "missing",
                "domains": {
                    domain: {
                        "status": "missing",
                        "asOfDate": None,
                        "recordCount": 0,
                    }
                    for domain in ("news", "events", "reports")
                },
                "news": [],
                "events": [],
                "reports": [],
            },
        )
        self.write_json(
            "data/provenance/index.json",
            {
                "schemaVersion": 1,
                "status": "missing",
                "generatedAt": None,
                "sourceCount": 0,
                "sources": [],
            },
        )

    def write_manifest(self, entries=None, overrides=None) -> None:
        if entries is None:
            entries = []
            for path in sorted((self.root / "data").rglob("*.json")):
                if path.name == "index.json" and path.parent == self.root / "data":
                    continue
                payload = path.read_bytes()
                entries.append(
                    {
                        "path": path.relative_to(self.root / "data").as_posix(),
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
        manifest = {
            "schemaVersion": 1,
            "generatedAt": None,
            "status": "partial",
            "datasets": {
                "pipeline": {"status": "missing", "recordCount": 0},
                "market": {
                    "status": "missing",
                    "recordCount": 0,
                    "asOfDate": None,
                },
                "etf": {"status": "missing", "recordCount": 0, "asOfDate": None},
                "financial": {
                    "status": "missing",
                    "recordCount": 0,
                    "asOfDate": None,
                },
                "news": {"status": "missing", "recordCount": 0, "asOfDate": None},
                "events": {
                    "status": "missing",
                    "recordCount": 0,
                    "asOfDate": None,
                },
                "reports": {
                    "status": "missing",
                    "recordCount": 0,
                    "asOfDate": None,
                },
            },
            "fileCount": len(entries),
            "files": entries,
        }
        if overrides:
            manifest.update(overrides)
        self.write_json("data/index.json", manifest)

    def assert_has_error(self, errors, fragment: str) -> None:
        self.assertTrue(
            any(fragment in error for error in errors),
            f"expected {fragment!r} in {errors!r}",
        )

    def test_accepts_closed_safe_manifest(self):
        self.assertEqual(validate_public_site(self.root), [])

    def test_requires_non_empty_root_index_html(self):
        (self.root / "index.html").unlink()
        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "missing or empty site entry point")

    def test_requires_reviewed_csp_and_no_referrer_metadata(self):
        original = (self.root / "index.html").read_text(encoding="utf-8")
        invalid_values = (
            ("<!doctype html>", "Content-Security-Policy"),
            (
                original.replace("script-src 'self'", "script-src 'self' 'unsafe-inline'"),
                "self-only contract",
            ),
            (
                original.replace("connect-src 'self'", "connect-src 'self' https:"),
                "self-only contract",
            ),
            (
                original.replace('content="no-referrer"', 'content="origin"'),
                "no-referrer policy",
            ),
        )
        for html, expected in invalid_values:
            with self.subTest(expected=expected):
                (self.root / "index.html").write_text(html, encoding="utf-8")
                self.assert_has_error(validate_public_site(self.root), expected)
        (self.root / "index.html").write_text(original, encoding="utf-8")

    def test_rejects_invalid_root_manifest_contract(self):
        invalid_values = (
            ({"schemaVersion": 2}, "schemaVersion must be integer 1"),
            ({"generatedAt": "2026-08-16"}, "timezone-aware ISO timestamp"),
            ({"status": "complete"}, "status is not a supported root status"),
            ({"datasets": None}, "datasets must be an object"),
            ({"fileCount": 99}, "fileCount does not match files"),
        )
        for overrides, expected_error in invalid_values:
            with self.subTest(overrides=overrides):
                self.write_manifest(overrides=overrides)
                errors = validate_public_site(self.root)
                self.assert_has_error(errors, expected_error)
        self.write_manifest()

    def test_rejects_invalid_dataset_contract_and_inconsistent_root_status(self):
        datasets = {
            "pipeline": {"status": "ready", "recordCount": 1},
            "market": {
                "status": "ready",
                "recordCount": 1,
                "asOfDate": "not-a-date",
            },
        }
        self.write_manifest(overrides={"datasets": datasets, "status": "ready"})

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "required domains")
        self.assert_has_error(errors, "datasets.market.asOfDate is invalid")

        self.write_manifest(overrides={"status": "ready"})
        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "status does not match dataset statuses")

    def test_accepts_nullable_generated_at_for_missing_source_data(self):
        self.write_manifest(overrides={"generatedAt": None})
        self.assertEqual(validate_public_site(self.root), [])

    def test_rejects_symlinks_without_following_them(self):
        target = Path(self.temporary_directory.name) / "outside.txt"
        target.write_text("outside", encoding="utf-8")
        try:
            os.symlink(target, self.root / "assets/link.txt")
        except (NotImplementedError, OSError):
            self.skipTest("symbolic links are unavailable")

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "symbolic links are forbidden")

    def test_rejects_symlinked_build_root(self):
        link = Path(self.temporary_directory.name) / "dist-link"
        try:
            os.symlink(self.root, link)
        except (NotImplementedError, OSError):
            self.skipTest("symbolic links are unavailable")

        errors = validate_public_site(link)
        self.assertEqual(
            errors, ["static-site build directory must not be a symbolic link"]
        )

    def test_rejects_forbidden_paths(self):
        for segment in (
            "private",
            "raw",
            "portfolio",
            "decision_journal",
            "inbox",
            ".codex-collection-inbox",
        ):
            path = self.root / segment / "leak.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")

        errors = validate_public_site(self.root)
        self.assertGreaterEqual(
            sum("paths are forbidden" in error for error in errors), 6
        )

    def test_rejects_unknown_extensions_and_source_maps(self):
        (self.root / "assets/runtime.bin").write_bytes(b"binary")
        (self.root / "assets/app.js.map").write_text("{}", encoding="utf-8")

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "file type is not allowlisted")
        self.assert_has_error(errors, "source maps are forbidden")

    def test_rejects_every_path_outside_the_fixed_artifact_layout(self):
        (self.root / "extra.txt").write_text("extra", encoding="utf-8")
        (self.root / "extra.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        unknown = self.root / "other/app.js"
        unknown.parent.mkdir()
        unknown.write_text("export {};", encoding="utf-8")

        errors = validate_public_site(self.root)
        self.assertGreaterEqual(
            sum("fixed Pages artifact layout" in error for error in errors), 3
        )

    def test_rejects_missing_or_unknown_fixed_data_paths(self):
        (self.root / "data/etf/index.json").unlink()
        self.write_json("data/etf/extra.json", {})
        self.write_manifest()

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "missing required fixed paths")
        self.assert_has_error(errors, "fixed public data path set")

    def test_rejects_hidden_artifacts_and_inline_source_maps(self):
        hidden = self.root / ".well-known/metadata.txt"
        hidden.parent.mkdir(parents=True)
        hidden.write_text("metadata", encoding="utf-8")
        (self.root / "assets/app.js").write_text(
            "//# sourceMappingURL=data:application/json;base64,e30=",
            encoding="utf-8",
        )

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "hidden artifacts are forbidden")
        self.assert_has_error(errors, "inline source maps are forbidden")

    def test_rejects_private_repository_paths_in_content(self):
        (self.root / "assets/app.js").write_text(
            "const source = 'data/raw/secret.json';",
            encoding="utf-8",
        )

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "public payload safety check failed")

    def test_rejects_per_file_and_total_size_overruns(self):
        errors = validate_public_site(
            self.root,
            max_file_bytes=1,
            max_total_bytes=10_000,
        )
        self.assert_has_error(errors, "per-file size limit")

        total_size = sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())
        errors = validate_public_site(
            self.root,
            max_file_bytes=10_000,
            max_total_bytes=total_size - 1,
        )
        self.assert_has_error(errors, "total size limit")

    def test_rejects_machine_paths_and_bearer_values_without_echoing_values(self):
        credential = "Bearer abcdefghijklmnop"
        (self.root / "assets/app.js").write_text(
            f"const local = '/Users/example/repo'; const credential = '{credential}';",
            encoding="utf-8",
        )

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "public payload safety check failed")
        self.assertNotIn(credential, "\n".join(errors))

    def test_allows_public_url_paths_but_rejects_real_local_paths(self):
        public_url = "https://example.invalid/data/raw/article?next=/home/x"
        (self.root / "assets/app.js").write_text(
            f"const source = {json.dumps(public_url)};", encoding="utf-8"
        )
        self.assertEqual(validate_public_site(self.root), [])

        for local_path in (
            "/Users/example/repo",
            "/home/example/repo",
            "/tmp/export.json",
            "/private/tmp/export.json",
            "/Volumes/research/data.json",
            "file:///Users/example/repo/data.json",
        ):
            with self.subTest(local_path=local_path):
                (self.root / "assets/app.js").write_text(
                    f"const source = {json.dumps(local_path)};", encoding="utf-8"
                )
                self.assert_has_error(
                    validate_public_site(self.root), "public payload safety check failed"
                )

    def test_json_site_path_scan_ignores_urls_but_rejects_private_repo_paths(self):
        content = json.loads((self.root / "data/content/index.json").read_text())
        content["status"] = "ready"
        content["domains"]["news"] = {
            "status": "ready",
            "asOfDate": "2026-08-16",
            "recordCount": 1,
        }
        content["news"] = [
            {
                "newsId": "news-1",
                "title": "public update",
                "summary": "public",
                "url": "https://example.invalid/data/raw/article?next=/home/x",
                "classificationKeywords": [],
            }
        ]
        self.write_json("data/content/index.json", content)
        manifest = json.loads((self.root / "data/index.json").read_text())
        manifest["datasets"]["news"] = {
            "status": "ready",
            "recordCount": 1,
            "asOfDate": "2026-08-16",
        }
        self.write_manifest(overrides={"datasets": manifest["datasets"]})
        self.assertEqual(validate_public_site(self.root), [])

        content["news"][0]["summary"] = "data/raw/secret.json"
        self.write_json("data/content/index.json", content)
        self.write_manifest(overrides={"datasets": manifest["datasets"]})
        self.assert_has_error(
            validate_public_site(self.root), "invalid or unsafe public JSON"
        )

    def test_reuses_public_payload_safety_for_every_json_file(self):
        self.write_json(
            "data/status/latest.json",
            {"status": "complete", "access_token": "must-not-publish"},
        )
        self.write_manifest()

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "invalid or unsafe public JSON")
        self.assertNotIn("must-not-publish", "\n".join(errors))

    def test_reuses_shared_sensitive_value_detection_for_text_assets(self):
        marker = "https://example.invalid/data?api_key=test-only-secret"
        (self.root / "assets/app.js").write_text(
            f"const sourceUrl = {json.dumps(marker)};",
            encoding="utf-8",
        )

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "public payload safety check failed")
        self.assertNotIn(marker, "\n".join(errors))

    def test_rejects_manifest_byte_and_digest_mismatches(self):
        entries = [
            {
                "path": "status/latest.json",
                "bytes": 1,
                "sha256": "0" * 64,
            }
        ]
        self.write_manifest(entries)

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "byte count does not match")
        self.assert_has_error(errors, "SHA-256 does not match")

    def test_rejects_uncovered_json(self):
        self.write_json("data/market/unlisted.json", {"rows": []})

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "does not cover every and only")

    def test_rejects_child_schema_version_and_shard_descriptor_drift(self):
        etf = json.loads((self.root / "data/etf/index.json").read_text())
        etf["schemaVersion"] = 2
        self.write_json("data/etf/index.json", etf)
        summary = json.loads((self.root / "data/market/summary.json").read_text())
        summary["shards"][0]["path"] = "market/queue-f.json"
        self.write_json("data/market/summary.json", summary)
        self.write_manifest()

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "data/etf/index.json: schemaVersion")
        self.assert_has_error(errors, "shards[0] does not match shard 0")

    def test_rejects_nested_content_type_even_when_manifest_hash_is_refreshed(self):
        content = json.loads((self.root / "data/content/index.json").read_text())
        content["status"] = "ready"
        content["domains"]["news"] = {
            "status": "ready",
            "asOfDate": "2026-08-16",
            "recordCount": 1,
        }
        content["news"] = [
            {
                "newsId": "news-1",
                "title": "public update",
                "url": "https://example.invalid/data/raw/article",
                "classificationKeywords": "not-a-list",
            }
        ]
        self.write_json("data/content/index.json", content)
        manifest = json.loads((self.root / "data/index.json").read_text())
        manifest["datasets"]["news"] = {
            "status": "ready",
            "recordCount": 1,
            "asOfDate": "2026-08-16",
        }
        self.write_manifest(overrides={"datasets": manifest["datasets"]})

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "classificationKeywords")

    def test_rejects_cross_file_dataset_count_mismatch(self):
        manifest = json.loads((self.root / "data/index.json").read_text())
        manifest["datasets"]["financial"] = {
            "status": "ready",
            "recordCount": 1,
            "asOfDate": "2026-08-16",
        }
        self.write_manifest(overrides={"datasets": manifest["datasets"]})

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "datasets.financial recordCount")

    def test_rejects_manifest_path_traversal(self):
        self.write_manifest(
            [{"path": "../outside.json", "bytes": 2, "sha256": "0" * 64}]
        )

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "invalid relative JSON path")

    def test_rejects_json_outside_data_directory(self):
        self.write_json("settings.json", {"public": True})

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "JSON artifacts outside data/")

    def test_rejects_invalid_or_duplicate_key_json(self):
        (self.root / "data/status/latest.json").write_text(
            '{"status":"complete","status":"blocked"}',
            encoding="utf-8",
        )
        self.write_manifest()

        errors = validate_public_site(self.root)
        self.assert_has_error(errors, "invalid or unsafe public JSON")


class PagesWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = PAGES_WORKFLOW.read_text(encoding="utf-8")

    def job(self, job_id: str) -> str:
        match = re.search(
            rf"(?ms)^  {re.escape(job_id)}:\n(?P<body>.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)",
            self.workflow,
        )
        self.assertIsNotNone(match, f"missing workflow job {job_id}")
        return match.group("body")

    def test_pages_build_runs_after_validation_for_every_trigger(self):
        build = self.job("build-pages")
        self.assertIn("needs: validate", build)
        self.assertNotRegex(build, re.compile(r"^    if:", re.MULTILINE))
        self.assertRegex(
            self.workflow,
            re.compile(
                r'(?ms)^  push:\n    branches:\n      - main\n      - "codex/\*\*"$'
            ),
        )
        self.assertIn("  pull_request:", self.workflow)
        self.assertNotIn("pages: write", build)
        self.assertIn("pages: read", build)

    def test_pages_build_exports_builds_validates_then_uploads(self):
        build = self.job("build-pages")
        required_in_order = (
            "python3 scripts/export_public_site.py --output-dir site/public/data",
            "pnpm --dir site build",
            "python3 scripts/validate_public_site.py site/dist",
            "uses: actions/configure-pages@v6",
            "uses: actions/upload-pages-artifact@v5",
        )
        offsets = [build.index(value) for value in required_in_order]
        self.assertEqual(offsets, sorted(offsets))
        self.assertIn("uses: actions/checkout@v7", build)
        self.assertIn("uses: actions/setup-python@v7", build)
        self.assertIn("uses: pnpm/setup@v2", build)
        self.assertIn("path: site/dist", build)
        self.assertIn(
            "if: github.event_name == 'push' && github.ref == 'refs/heads/main'",
            build,
        )

    def test_deployment_has_only_required_write_permissions_and_serialization(self):
        deploy = self.job("deploy-pages")
        self.assertIn("needs: build-pages", deploy)
        self.assertIn(
            "if: github.event_name == 'push' && github.ref == 'refs/heads/main'",
            deploy,
        )
        self.assertIn("actions: read", deploy)
        self.assertIn("pages: write", deploy)
        self.assertIn("id-token: write", deploy)
        self.assertNotIn("contents: write", deploy)
        self.assertIn("name: github-pages", deploy)
        self.assertIn("uses: actions/deploy-pages@v5", deploy)

    def test_workflow_cancels_older_runs_on_the_same_ref(self):
        self.assertIn("group: pages-${{ github.ref }}", self.workflow)
        self.assertIn("cancel-in-progress: true", self.workflow)


if __name__ == "__main__":
    unittest.main()
