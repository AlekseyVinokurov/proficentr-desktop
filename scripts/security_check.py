"""Small project policy supplementing Bandit, gitleaks and pip-audit, not a sandbox."""
import ast
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    policy = json.loads((root / "security-policy.json").read_text())
    failures = []
    for path in (root / "src").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""] + [(node.module or "") + "." + n.name for n in node.names]
                for module in modules:
                    if any(word in module.casefold() for word in ("telemetry", "sentry", "analytics", "posthog", "segment", "mixpanel", "opentelemetry")):
                        failures.append(f"{relative}: telemetry import")
                    if module.split('.')[0] in {"socket", "ssl", "http", "urllib", "requests", "httpx", "aiohttp", "websocket", "ftplib", "smtplib", "xmlrpc"} or "QtNetwork" in module or "QtWebEngine" in module:
                        if relative != "src/proficentr/updates.py" or not (module == "urllib.request" or module == "urllib.parse" or module.startswith("urllib.parse.")):
                            failures.append(f"{relative}: unapproved network import {module}")
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name in {"exec", "eval", "compile", "__import__", "os.system", "os.popen", "importlib.import_module"} or name.endswith((".extractall", ".openUrl")):
                    failures.append(f"{relative}: unsafe call {name}")
                if any(k.arg == "shell" and not (isinstance(k.value, ast.Constant) and k.value.value is False) for k in node.keywords):
                    failures.append(f"{relative}: shell execution")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if 'https://' in node.value or 'http://' in node.value:
                    if relative != 'src/proficentr/updates.py' or node.value not in policy['url_literals']:
                        failures.append(f"{relative}: unapproved URL literal")
        if relative == "src/proficentr/updates.py":
            values = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
                      if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                      and isinstance(n.value, ast.Constant)}
            if values.get("REPOSITORY") != policy["repository"]:
                failures.append("Update repository changed")
            if hashlib.sha256(bytes.fromhex(values["PUBLIC_KEY"])).hexdigest() != policy["key_sha256"]:
                failures.append("Update trust key changed")
    return failures


if __name__ == "__main__":
    errors = check()
    print("\n".join(errors) if errors else "Security policy OK")
    sys.exit(bool(errors))
