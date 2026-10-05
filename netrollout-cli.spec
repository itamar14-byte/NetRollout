# PyInstaller recipe for netrollout-cli.exe — the headless rollout tool as
# one Windows executable (Python, Netmiko and the rest bundled inside).
#
#   pyinstaller --clean --noconfirm netrollout-cli.spec      (repo root, dev venv)
#   → dist/netrollout-cli.exe
#
# The web app's stack is excluded on purpose: the CLI never loads it (guarded
# by tests/unit/test_cli.py), and if anything starts importing it again the
# .exe fails at once instead of quietly growing.
# Netmiko's ntc_templates data isn't bundled: it's only used for TextFSM
# parsing, which NetRollout never asks for.

WEB_STACK = [
	"flask", "flask_login", "flask_session", "flask_wtf", "flask_limiter",
	"werkzeug", "jinja2", "waitress", "sqlalchemy", "alembic", "psycopg2",
	"redis", "prometheus_client", "prometheus_flask_exporter", "ldap3",
	"pyotp", "qrcode", "PIL", "src.webapp", "src.db",
]

a = Analysis(
	["src/cli.py"],
	pathex=["."],                 # "src" is imported as a package
	datas=[("VERSION", ".")],     # the version (src/runtime.py reads it)
	excludes=WEB_STACK,
	noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
	pyz,
	a.scripts,
	a.binaries,
	a.datas,
	name="netrollout-cli",
	console=True,
	upx=False,                    # packed executables are flagged more by antivirus
	debug=False,
)
