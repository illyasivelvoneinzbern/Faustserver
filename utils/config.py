"""
配置加载模块：从 config.yaml 和环境变量读取所有配置项。
"""

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()


def _resolve_env(value: str) -> str:
    """解析 ${ENV_VAR} 形式的占位符"""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        env_var = value[2:-1]
        return os.getenv(env_var, value)
    return value


def _resolve_dict(d: dict) -> dict:
    """递归解析字典中的环境变量占位符"""
    result = {}
    for k, v in d.items():
        if isinstance(v, dict):
            result[k] = _resolve_dict(v)
        elif isinstance(v, list):
            result[k] = [_resolve_env(item) if isinstance(item, str) else item for item in v]
        elif isinstance(v, str):
            result[k] = _resolve_env(v)
        else:
            result[k] = v
    return result


def load_config(config_path: str = "config.yaml") -> dict[str, Any]:
    """加载并解析配置文件"""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件未找到: {path.absolute()}")

    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    return _resolve_dict(raw)


# 全局配置实例（懒加载）
_config: dict[str, Any] | None = None


def get_config(config_path: str = "config.yaml") -> dict[str, Any]:
    """获取全局配置（单例模式）"""
    global _config
    if _config is None:
        _config = load_config(config_path)
    return _config


def reload_config(config_path: str = "config.yaml") -> dict[str, Any]:
    """重新加载配置"""
    global _config
    _config = load_config(config_path)
    return _config
