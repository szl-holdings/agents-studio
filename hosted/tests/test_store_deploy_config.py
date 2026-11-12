"""Contracts for the store Worker's checked-in deploy configuration.

These are offline source checks. They do not prove what is deployed; the deploy
workflow's post-deploy readback is the live evidence.
"""

import json
import re
import sqlite3
import tomllib
import unittest
from pathlib import Path

HOSTED = Path(__file__).resolve().parents[1]
EDGE = HOSTED / "edge"
WORKFLOW = HOSTED.parent / ".github" / "workflows" / "hosted-deploy.yml"
CONFIG = tomllib.loads((EDGE / "wrangler.toml").read_text(encoding="utf-8"))
# Live values read back from the Cloudflare account settings export on 2026-11-06.
LIVE_DATABASE_ID = "27edfa50-be82-4fa1-b0c8-0c630c7db3ea"
LIVE_COMPATIBILITY_DATE = "2026-10-01"
REQUIRED_SECRETS = ["STORE_SECRET", "ADMISSION_SECRET"]
# Read by store.mjs but not declared in wrangler.toml: each one only enables an
# optional endpoint. METRICS_SECRET is set on the live Worker and every deploy keeps
# it; the Snowflake dispatch pair stays unset until a separately reviewed change admits it.
OPTIONAL_UNSET = {"METRICS_SECRET", "SNOWFLAKE_DISPATCH_SECRET", "SNOWFLAKE_EXPORTER_REVISION"}
ADDITIVE_STATEMENT = re.compile(
    r"(CREATE TABLE IF NOT EXISTS|CREATE (UNIQUE )?INDEX IF NOT EXISTS|INSERT OR IGNORE INTO)\s")


def statements(sql):
    without_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [part.strip() for part in without_comments.split(";") if part.strip()]


def schema_snapshot(connection):
    return connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name").fetchall()


class WranglerConfigTests(unittest.TestCase):
    def test_worker_identity_matches_the_live_store(self):
        self.assertEqual(CONFIG["name"], "szl-agents-studio-store")
        self.assertEqual(CONFIG["main"], "store.mjs")
        self.assertTrue((EDGE / CONFIG["main"]).is_file())
        self.assertEqual(CONFIG["compatibility_date"], LIVE_COMPATIBILITY_DATE)
        self.assertNotIn("account_id", CONFIG)

    def test_only_the_committed_module_is_uploaded(self):
        self.assertIs(CONFIG["no_bundle"], True)
        self.assertIs(CONFIG["find_additional_modules"], False)

    def test_routes_are_not_managed_and_preview_urls_stay_off(self):
        # A `routes` entry would make `wrangler deploy` take over the Custom Domain
        # (and need zone permissions); preview URLs would serve old versions on live D1.
        self.assertFalse({"route", "routes"} & CONFIG.keys())
        self.assertIs(CONFIG["workers_dev"], True)
        self.assertIs(CONFIG["preview_urls"], False)
        self.assertEqual(CONFIG["observability"], {"enabled": False})

    def test_plain_variables_are_exact_and_never_secrets(self):
        self.assertEqual(CONFIG["vars"], {"REQUIRE_MODEL_BINDING": "1", "SOURCE_REVISION": "UNKNOWN"})
        for name in CONFIG["vars"]:
            self.assertNotRegex(name, r"SECRET|TOKEN|PASSWORD|CREDENTIAL")

    def test_required_secrets_are_declared_by_name_only(self):
        self.assertEqual(CONFIG["secrets"], {"required": REQUIRED_SECRETS})
        text = (EDGE / "wrangler.toml").read_text(encoding="utf-8")
        for name in REQUIRED_SECRETS + sorted(OPTIONAL_UNSET):
            self.assertIsNone(re.search(rf"^\s*{name}\s*=", text, re.MULTILINE), name)

    def test_d1_binding_targets_the_identified_database(self):
        self.assertEqual(CONFIG["d1_databases"], [{
            "binding": "DB", "database_name": "szl-agents-studio-store",
            "database_id": LIVE_DATABASE_ID, "migrations_dir": "migrations"}])

    def test_every_binding_the_worker_reads_is_declared_or_documented_optional(self):
        source = (EDGE / CONFIG["main"]).read_text(encoding="utf-8")
        used = set(re.findall(r"\benv\.([A-Z][A-Z0-9_]*)", source))
        declared = ({entry["binding"] for entry in CONFIG["d1_databases"]} | set(CONFIG["vars"])
                    | set(CONFIG["secrets"]["required"]))
        self.assertEqual(used - declared, OPTIONAL_UNSET)
        self.assertEqual(declared - used, set())


class MigrationTests(unittest.TestCase):
    MIGRATIONS = sorted((EDGE / CONFIG["d1_databases"][0]["migrations_dir"]).glob("*.sql"))

    def test_migrations_are_ordered_and_named_for_wrangler(self):
        names = [path.name for path in self.MIGRATIONS]
        self.assertEqual(names, ["0002_admission_key_epoch.sql", "0003_run_model_bindings.sql",
                                 "0004_snowflake_dispatch_hold.sql"])
        numbers = [int(name[:4]) for name in names]
        self.assertEqual(numbers, sorted(set(numbers)))

    def test_ci_applied_migrations_are_additive_and_idempotent_statements(self):
        # CI applies these unattended after environment approval, and the live
        # d1_migrations bookkeeping was never read back, so 0002/0003 may run again.
        for path in self.MIGRATIONS:
            for statement in statements(path.read_text(encoding="utf-8")):
                self.assertRegex(statement, ADDITIVE_STATEMENT, f"{path.name}: {statement[:60]}")

    def test_reapplying_every_migration_to_the_full_schema_changes_nothing(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.executescript((EDGE / "schema.sql").read_text(encoding="utf-8"))
        connection.execute("INSERT INTO admission_key_epochs VALUES ('2026-11-05', ?)", ("a" * 64,))
        before = schema_snapshot(connection)
        for _round in range(2):
            for path in self.MIGRATIONS:
                connection.executescript(path.read_text(encoding="utf-8"))
        self.assertEqual(schema_snapshot(connection), before)
        for table in ("studio_schema", "admission_epoch_schema", "snowflake_dispatch_schema"):
            self.assertEqual(connection.execute(f"SELECT id, version FROM {table}").fetchall(), [(1, 1)])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM admission_key_epochs").fetchone(), (1,))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM snowflake_dispatch_holds").fetchone(), (0,))

    def test_migrations_bring_a_version_one_database_to_the_full_schema(self):
        full = sqlite3.connect(":memory:")
        self.addCleanup(full.close)
        full.executescript((EDGE / "schema.sql").read_text(encoding="utf-8"))
        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        legacy.executescript((EDGE / "schema.sql").read_text(encoding="utf-8"))
        for table in ("snowflake_dispatch_holds", "snowflake_dispatch_schema", "run_model_bindings",
                      "admission_key_epochs", "admission_epoch_schema"):
            legacy.execute(f"DROP TABLE {table}")
        for path in self.MIGRATIONS:
            legacy.executescript(path.read_text(encoding="utf-8"))
        self.assertEqual(schema_snapshot(legacy), schema_snapshot(full))


class PinnedToolingTests(unittest.TestCase):
    def test_wrangler_is_pinned_exactly_by_package_json_and_lockfile(self):
        package = json.loads((EDGE / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((EDGE / "package-lock.json").read_text(encoding="utf-8"))
        version = package["devDependencies"]["wrangler"]
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        self.assertIs(package["private"], True)
        self.assertNotIn("dependencies", package)
        self.assertEqual(lock["packages"][""]["devDependencies"], package["devDependencies"])
        self.assertEqual(lock["packages"]["node_modules/wrangler"]["version"], version)
        for name, entry in lock["packages"].items():
            if name:
                self.assertTrue(entry["resolved"].startswith("https://registry.npmjs.org/"), name)
                self.assertTrue(entry["integrity"].startswith("sha512-"), name)


class DeployWorkflowTests(unittest.TestCase):
    TEXT = WORKFLOW.read_text(encoding="utf-8")

    def test_every_action_is_pinned_by_full_commit_sha(self):
        uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", self.TEXT, re.MULTILINE)
        self.assertIn("cloudflare/wrangler-action", " ".join(uses))
        for reference in uses:
            self.assertRegex(reference, r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")

    def test_token_scope_is_read_only_and_credentials_come_from_the_environment(self):
        self.assertRegex(self.TEXT, r"(?m)^permissions:\n  contents: read$")
        self.assertNotRegex(self.TEXT, r":\s*write\b")
        self.assertNotIn("pull_request_target", self.TEXT)
        self.assertRegex(self.TEXT, r"environment:\n\s+name: cloudflare-production\n")
        self.assertIn("vars.SZL_STORE_DEPLOY_ENABLED == '1'", self.TEXT)
        self.assertIn("github.ref == 'refs/heads/main'", self.TEXT)
        self.assertEqual(self.TEXT.count("secrets.CLOUDFLARE_API_TOKEN }}"),
                         self.TEXT.count("apiToken:"))

    def test_tests_and_restore_point_precede_migration_deploy_and_readback(self):
        order = ["node --test hosted/tests/test_store.mjs", "wrangler deploy --dry-run",
                 "needs: verify", "d1 time-travel info DB", "d1 migrations apply DB --remote",
                 "command: deploy", "python -B store_deploy_readback.py"]
        positions = [self.TEXT.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))

    def test_full_commit_sha_is_injected_and_read_back(self):
        self.assertIn("SOURCE_REVISION: ${{ github.sha }}", self.TEXT)
        self.assertIn("vars: SOURCE_REVISION", self.TEXT)
        self.assertIn('--expect-revision "$EXPECTED_REVISION"', self.TEXT)
        self.assertIn("EXPECTED_REVISION: ${{ github.sha }}", self.TEXT)

    def test_dependencies_install_only_from_the_lockfile(self):
        self.assertIn("npm ci", self.TEXT)
        self.assertNotRegex(self.TEXT, r"npm (install|i)\b")
        self.assertNotRegex(self.TEXT, r"pip install")


if __name__ == "__main__":
    unittest.main()
