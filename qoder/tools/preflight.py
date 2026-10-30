#!/usr/bin/env python
"""提交前自检：汇总提测字段（仓库/分支/SHA/站点），检查凭据泄漏、必需文件、验证证据与站点可达性。

用法（在 qoder/ 目录下）：
    python tools/preflight.py
    python tools/preflight.py --live          # 额外检查本机 8000/8080/8081 服务
    python tools/preflight.py --allow-dirty   # 提交前先自检，工作区仍有改动时不判失败

退出码 0 表示可以提测；证据写入 qoder/artifacts/preflight.json。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

QODER_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = QODER_DIR.parent
DEFAULT_SITE_URL = "https://literature-desk-olizvin1uxl.qoder.zone"
SITE_TITLE = "文献阅读工作台"

REQUIRED_FILES = [
    "qoder/README.md",
    "qoder/.env.example",
    "qoder/app/server.py",
    "qoder/app/index.html",
    "qoder/tools/embed_server.py",
    "qoder/tools/verify_powercontext.py",
    "qoder/tools/verify_literature.py",
    "qoder/docs/verification-example.md",
    "qoder/docs/qoder-dev-log.md",
    "qoder/docs/submission.md",
    "qoder/site/index.html",
    "qoder/site/data/demo.json",
]

SECRET_PATTERNS = {
    "github_token": re.compile(r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    "aws_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "credential_literal": re.compile(
        r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"]?"
        r"(?!replace-with|<|your[-_]|xxx|\$\{|example|placeholder|none|null)[A-Za-z0-9/+=_-]{12,}"
    ),
}
LOCAL_PATH = re.compile(r"[A-Za-z]:[\\/]Users[\\/](?!<user>)[^\\/\s\"']+")
TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".html",
    ".css",
    ".js",
    ".json",
    ".txt",
    ".example",
    ".gitignore",
    ".site",
    ".yaml",
    ".yml",
}


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_DIR), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} -> {result.stderr.strip()}")
    return result.stdout.strip()


def collect_git() -> dict:
    remote = git("remote", "get-url", "origin")
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    commit = git("rev-parse", "HEAD")
    dirty = git("status", "--porcelain", "--", "qoder").splitlines()
    return {"remote": remote, "branch": branch, "commit": commit, "dirty": dirty}


def candidate_files() -> list[Path]:
    listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "qoder")
    paths = [REPO_DIR / item for item in listed.split("\0") if item]
    return [path for path in paths if path.is_file()]


def scan_secrets(files: list[Path]) -> list[dict]:
    findings: list[dict] = []
    for path in files:
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {".gitignore"}:
            continue
        if path.stat().st_size > 2 * 1024 * 1024:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(REPO_DIR).as_posix()
        for label, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                findings.append({"file": rel, "kind": label, "line": text.count("\n", 0, match.start()) + 1})
        for match in LOCAL_PATH.finditer(text):
            findings.append({"file": rel, "kind": "local_absolute_path", "line": text.count("\n", 0, match.start()) + 1})
    return findings


def fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "literature-desk-preflight"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def check_site(base_url: str) -> dict:
    result: dict = {"base_url": base_url}
    for name, path, marker in [
        ("index", "/", SITE_TITLE),
        ("demo_data", "/data/demo.json", '"documents"'),
        ("styles", "/styles.css", "--"),
        ("script", "/app.js", "function"),
    ]:
        try:
            status, body = fetch(base_url + path)
        except Exception as error:  # 网络不可达时同样视为失败
            result[name] = {"path": path, "error": str(error)}
            continue
        result[name] = {
            "path": path,
            "status": status,
            "marker_found": marker.encode("utf-8") in body,
            "bytes": len(body),
        }
    result["passed"] = all(
        isinstance(item, dict) and item.get("status") == 200 and item.get("marker_found")
        for key, item in result.items()
        if key not in {"base_url", "passed"}
    )
    return result


def check_evidence() -> dict:
    evidence: dict = {}
    for name in ["verify-powercontext.json", "verify-literature.json"]:
        path = QODER_DIR / "artifacts" / name
        if not path.is_file():
            evidence[name] = {"exists": False, "passed": False}
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        evidence[name] = {"exists": True, "passed": data.get("passed") is True, "checked_at": data.get("checked_at")}
    evidence["passed"] = all(item.get("passed") for item in evidence.values())
    return evidence


def check_live() -> dict:
    targets = {
        "powercontext": ("http://127.0.0.1:8000/v1/capabilities", "search_modes"),
        "workbench": ("http://127.0.0.1:8080/api/health", "embedding"),
        "embedding": ("http://127.0.0.1:8081/health", "dimension"),
    }
    live: dict = {}
    for name, (url, marker) in targets.items():
        try:
            status, body = fetch(url)
        except Exception as error:
            live[name] = {"url": url, "error": str(error), "ok": False}
            continue
        live[name] = {"url": url, "status": status, "ok": status == 200 and marker.encode() in body}
    return live


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", default=DEFAULT_SITE_URL)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--output", default=str(QODER_DIR / "artifacts" / "preflight.json"))
    args = parser.parse_args()

    site_url = args.site.rstrip("/")

    report: dict = {"checked_at": datetime.now(timezone.utc).isoformat(), "site_url": site_url}
    blocking: list[str] = []
    warnings: list[str] = []

    git_info = collect_git()
    report["git"] = git_info
    if git_info["dirty"] and not args.allow_dirty:
        blocking.append(f"working tree has {len(git_info['dirty'])} uncommitted path(s) under qoder/; commit and push before submitting")

    report["submission"] = {
        "repo": git_info["remote"],
        "branch": git_info["branch"],
        "commit": git_info["commit"],
        "site": site_url,
    }

    missing = [rel for rel in REQUIRED_FILES if not (REPO_DIR / rel).is_file()]
    report["files"] = {"checked": len(REQUIRED_FILES), "missing": missing}
    if missing:
        blocking.append(f"missing files: {', '.join(missing)}")

    env_path = "qoder/.env"
    env_tracked = subprocess.run(
        ["git", "-C", str(REPO_DIR), "ls-files", "--error-unmatch", env_path],
        capture_output=True,
    ).returncode == 0
    env_ignored = subprocess.run(
        ["git", "-C", str(REPO_DIR), "check-ignore", "-q", env_path], capture_output=True
    ).returncode == 0
    report["env"] = {"tracked": env_tracked, "ignored": env_ignored}
    if env_tracked:
        blocking.append(".env is tracked by git; remove it from the index before submitting")
    if not env_ignored:
        warnings.append(".env is not matched by .gitignore")

    files = candidate_files()
    findings = scan_secrets(files)
    report["secrets"] = {"scanned": len(files), "findings": findings}
    if findings:
        blocking.append(f"credential or local path scan found {len(findings)} hit(s)")

    site = check_site(site_url)
    report["site"] = site
    if not site["passed"]:
        blocking.append("public site check failed")

    evidence = check_evidence()
    report["evidence"] = evidence
    if not evidence["passed"]:
        blocking.append("verification evidence missing or not passed")

    if args.live:
        live = check_live()
        report["live"] = live
        if not all(item.get("ok") for item in live.values()):
            warnings.append("some local services are not reachable; run before capturing screenshots")

    report["blocking"] = blocking
    report["warnings"] = warnings
    report["passed"] = not blocking
    report["submit_ready"] = report["passed"] and not git_info["dirty"]

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report["submission"], ensure_ascii=True, indent=2))
    print(f"files={report['files']['checked']} scanned={report['secrets']['scanned']} secrets={len(findings)}")
    print(f"site={'ok' if site['passed'] else 'fail'} evidence={'ok' if evidence['passed'] else 'fail'} dirty={len(git_info['dirty'])}")
    for item in blocking:
        print(f"BLOCK: {item}", file=sys.stderr)
    for item in warnings:
        print(f"WARN: {item}", file=sys.stderr)
    print(f"evidence -> {output}")
    if not report["passed"]:
        print("FAIL: not ready to submit", file=sys.stderr)
        return 1
    print("PASS: preflight ok" + ("" if report["submit_ready"] else " (working tree dirty; commit then rerun)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
