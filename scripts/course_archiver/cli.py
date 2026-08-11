from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
from pathlib import Path
from typing import Dict

from course_scraper import LoginTimeoutError

from .dedao import DEFAULT_DEDAO_UI_NOISE_PATTERNS, EnhancedDedaoScraper
from .storage import ArchiveStore
from .validator import validate_archive


def _load_config(path: Path) -> Dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("course_url", "course_title", "output_root", "profile_dir", "storage_state_path"):
        if not data.get(key):
            raise ValueError(f"配置缺少字段: {key}")
    if data.get("platform", "dedao") != "dedao":
        raise ValueError("当前发布包仅内置 dedao 适配器；其他平台请复用 ArchiveStore 和正文清洗器实现适配器")
    patterns = data.get("ui_noise_patterns", [])
    if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
        raise ValueError("ui_noise_patterns 必须是正则表达式字符串列表")
    return data


def _resolve(value: str, config_path: Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (config_path.parent / path).resolve()


def secure_private_directory(path: Path) -> None:
    """将浏览器 profile 和会话快照限制为当前用户，绝不把凭据写入交付目录。"""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.chmod(0o700)
        except OSError:
            pass
        return
    user = getpass.getuser()
    commands = (
        ["icacls.exe", str(path), "/inheritance:r", "/grant:r", f"{user}:F", "/T", "/C", "/Q"],
        ["icacls.exe", str(path), "/grant:r", f"{user}:(OI)(CI)F", "/C", "/Q"],
    )
    for command in commands:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if completed.returncode != 0:
            raise RuntimeError(f"无法限制会话目录权限: {completed.stderr or completed.stdout}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="得到课程全量归档与增量更新")
    parser.add_argument("--config", required=True, help="JSON 配置文件路径")
    parser.add_argument("--mode", choices=("login", "full", "incremental", "repair", "validate"), default="incremental")
    parser.add_argument("--headless", action="store_true", help="后台运行浏览器；login 始终可见")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    config_path = Path(args.config).resolve()
    config = _load_config(config_path)
    output_root = _resolve(str(config["output_root"]), config_path)
    profile_dir = _resolve(str(config["profile_dir"]), config_path)
    storage_state_path = _resolve(str(config["storage_state_path"]), config_path)
    noise_patterns = tuple(DEFAULT_DEDAO_UI_NOISE_PATTERNS) + tuple(config.get("ui_noise_patterns", []))
    store = ArchiveStore(output_root, str(config["course_title"]), noise_patterns)
    scraper = EnhancedDedaoScraper(
        start_url=str(config["course_url"]),
        output_root=output_root,
        course_title=str(config["course_title"]),
        profile_dir=profile_dir,
        storage_state_path=storage_state_path,
        headless=bool(args.headless),
        login_timeout=int(config.get("login_timeout_seconds", 600)),
        recent_recheck_count=int(config.get("recent_recheck_count", 7)),
        ui_noise_patterns=tuple(config.get("ui_noise_patterns", [])),
    )
    try:
        if args.mode == "login":
            secure_private_directory(profile_dir.parent)
            print(json.dumps(scraper.login_only(), ensure_ascii=False, indent=2))
            return 0
        if args.mode == "repair":
            print(json.dumps({"status": "SUCCESS", **store.repair_local_metadata()}, ensure_ascii=False, indent=2))
            return 0
        if args.mode == "validate":
            result = validate_archive(store)
            store.write_status("SUCCESS" if result["status"] == "PASS" else "FAILED", mode="validate", archive_validation=result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "PASS" else 2
        secure_private_directory(profile_dir.parent)
        result = scraper.run_archive(mode=args.mode)
        if result["status"] == "SUCCESS":
            result["archive_validation"] = validate_archive(store)
            if result["archive_validation"]["status"] != "PASS":
                result["status"] = "INCOMPLETE"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "SUCCESS" else 2
    except LoginTimeoutError as exc:
        payload = {"status": "AUTH_REQUIRED", "error": str(exc)}
        store.write_status(payload["status"], error=payload["error"])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 20
    except Exception as exc:
        payload = {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
        store.write_status(payload["status"], error=payload["error"])
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
