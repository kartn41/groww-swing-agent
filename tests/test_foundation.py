"""env loader, log redaction, static-IP guard, universe loader."""
import io, logging, os, tempfile, unittest
from app.env import load_env_file
from app.logging_setup import RedactingFormatter, redact
from app.net_guard import check_static_ip
from app.universe import UniverseError, load_universe


class TestEnv(unittest.TestCase):
    def write(self, text, mode=0o600):
        p = os.path.join(tempfile.mkdtemp(), ".env")
        with open(p, "w") as f:
            f.write(text)
        os.chmod(p, mode); return p

    def test_parses_and_does_not_override(self):
        p = self.write('# c\nA=1\nexport B="two words"\nC=\'x\'\nbad line\nA2 = spaced \n')
        env = {"A": "keep"}
        r = load_env_file(p, env)
        self.assertEqual(env, {"A": "keep", "B": "two words", "C": "x", "A2": "spaced"})
        self.assertNotIn("A", r["loaded"])

    def test_warns_on_loose_permissions(self):
        r = load_env_file(self.write("A=1\n", 0o644), {})
        self.assertTrue(any("chmod 600" in w for w in r["warnings"]))

    def test_missing_file_is_paper_default(self):
        r = load_env_file("/nonexistent/.env", {})
        self.assertTrue(r["warnings"])


class TestRedaction(unittest.TestCase):
    SECRET = "S3CRETVALUE123"
    JWT = "eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.abcdefGHIJ"

    def test_redacts_values_and_jwts(self):
        out = redact(f"token={self.SECRET} jwt={self.JWT}", [self.SECRET])
        self.assertNotIn(self.SECRET, out); self.assertNotIn(self.JWT, out)

    def test_redacts_inside_tracebacks(self):
        stream = io.StringIO()
        h = logging.StreamHandler(stream)
        h.setFormatter(RedactingFormatter("%(message)s", lambda: [self.SECRET]))
        lg = logging.getLogger("redact-test"); lg.addHandler(h); lg.propagate = False
        try:
            raise RuntimeError(f"auth failed for key {self.SECRET}")
        except RuntimeError:
            lg.exception("login error %s", self.JWT)
        text = stream.getvalue()
        self.assertIn("auth failed", text)
        self.assertNotIn(self.SECRET, text); self.assertNotIn(self.JWT, text)


class TestIpGuard(unittest.TestCase):
    def test_match(self):
        self.assertTrue(check_static_ip("13.1.2.3", lambda: "13.1.2.3\n")[0])

    def test_mismatch_missing_error_garbage_all_block(self):
        self.assertFalse(check_static_ip("13.1.2.3", lambda: "13.1.2.4")[0])
        self.assertFalse(check_static_ip("", lambda: "13.1.2.3")[0])
        def boom(): raise OSError("offline")
        self.assertFalse(check_static_ip("13.1.2.3", boom)[0])
        self.assertFalse(check_static_ip("13.1.2.3", lambda: "<html>")[0])


def write_universe(rows, header=("Company Name", "Industry", "Symbol", "Series", "ISIN Code")):
    p = os.path.join(tempfile.mkdtemp(), "u.csv")
    with open(p, "w") as f:
        f.write(",".join(header) + "\n")
        for r in rows:
            f.write(",".join(r) + "\n")
    return p


class TestUniverse(unittest.TestCase):
    def rows(self, n, series="EQ"):
        return [(f"C{i}", "IT", f"S{i}", series, f"I{i}") for i in range(n)]

    def test_loads_and_skips_non_eq_and_dupes(self):
        rows = self.rows(50) + [("X", "IT", "S0", "EQ", "I0"), ("Y", "IT", "BE1", "BE", "I9")]
        uni, w = load_universe(write_universe(rows))
        self.assertEqual(len(uni), 50); self.assertEqual(uni[0].sector, "IT")

    def test_fails_closed(self):
        with self.assertRaises(UniverseError):
            load_universe("/nonexistent.csv")
        with self.assertRaises(UniverseError):
            load_universe(write_universe(self.rows(10)))
        with self.assertRaises(UniverseError):
            load_universe(write_universe(self.rows(50), header=("Name", "Sector", "Ticker", "Series", "ISIN")))

    def test_stale_warning(self):
        p = write_universe(self.rows(50))
        _, w = load_universe(p, now=os.path.getmtime(p) + 60 * 86400)
        self.assertTrue(w)


if __name__ == "__main__":
    unittest.main()
