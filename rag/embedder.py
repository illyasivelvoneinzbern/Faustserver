"""
向量嵌入模块：包装 OpenAI / 本地 Embedding 模型。
本地模型默认使用 ModelScope（阿里魔搭）下载，国内直连无需代理。
"""

import logging
import os
from pathlib import Path
from typing import Any, Optional

from langchain_openai import OpenAIEmbeddings

logger = logging.getLogger(__name__)

# 本地模型缓存目录
LOCAL_MODEL_DIR = Path("./data/models")


def _download_from_modelscope(model_id: str, cache_dir: str) -> str:
    """从 ModelScope（阿里魔搭）下载模型，国内直连免代理。

    Returns:
        模型本地路径
    """
    try:
        from modelscope import snapshot_download
    except ImportError:
        logger.error(
            "需要安装 modelscope: pip install modelscope"
        )
        raise

    logger.info(f"正在从 ModelScope 下载模型: {model_id} ...")
    local_path = snapshot_download(
        model_id=model_id,
        cache_dir=cache_dir,
    )
    logger.info(f"模型已下载到: {local_path}")
    return local_path


def _create_local_embedder(config: dict) -> Any:
    """创建本地 Embedding 模型。

    下载策略（按优先级）：
    1. 如已设置 HF_ENDPOINT 环境变量 → 走 HuggingFace 镜像（hf-mirror.com）
    2. 否则 → 走 ModelScope（阿里魔搭，国内直连）
    3. 如果模型已缓存在本地 → 直接加载，不重复下载
    """
    model_name = config.get("model", "BAAI/bge-m3")

    # 如果显式设置了 HuggingFace 镜像，优先走镜像
    hf_endpoint = config.get("hf_endpoint") or os.environ.get("HF_ENDPOINT")

    if hf_endpoint:
        # 走 HuggingFace 镜像
        os.environ.setdefault("HF_ENDPOINT", hf_endpoint)
        logger.info(f"使用 HuggingFace 镜像: {hf_endpoint}")
        model_path_or_name = model_name
    else:
        # 默认走 ModelScope（国内友好）
        # BAAI/bge-m3 在 ModelScope 上的对应 ID
        modelscope_id = config.get("modelscope_id", f"BAAI/{model_name.split('/')[-1]}")
        LOCAL_MODEL_DIR.mkdir(parents=True, exist_ok=True)

        # 检查是否已缓存
        expected_dir = LOCAL_MODEL_DIR / model_name.replace("/", "--")
        if expected_dir.exists() and any(expected_dir.iterdir()):
            logger.info(f"模型已缓存，直接加载: {expected_dir}")
            model_path_or_name = str(expected_dir)
        else:
            model_path_or_name = _download_from_modelscope(
                modelscope_id,
                cache_dir=str(LOCAL_MODEL_DIR),
            )

    # 加载 HuggingFaceEmbeddings
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
    except ImportError:
        try:
            from langchain_community.embeddings import HuggingFaceEmbeddings
        except ImportError:
            logger.error(
                "本地 Embedding 需要安装: pip install langchain-huggingface"
            )
            raise

    # 如果模型已在本地，禁止联网下载（避免被墙）
    model_kwargs = {"device": config.get("device", "cpu")}
    if Path(model_path_or_name).is_dir():
        model_kwargs["local_files_only"] = True
        logger.info(f"使用本地模型（禁止联网）: {model_path_or_name}")

    return HuggingFaceEmbeddings(
        model_name=model_path_or_name,
        model_kwargs=model_kwargs,
        encode_kwargs={"normalize_embeddings": True},
    )


def create_embedder(config: dict) -> Any:
    """
    根据配置创建 Embedding 模型实例。

    支持：
    - openai: OpenAI / DeepSeek 兼容 API
    - local: 本地模型（ModelScope 下载 或 HuggingFace 镜像）

    配置示例：
    {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "api_key": "${OPENAI_API_KEY}",
        "base_url": "https://api.openai.com/v1"
    }
    """
    provider = config.get("provider", "openai")

    if provider == "openai":
        return OpenAIEmbeddings(
            model=config.get("model", "text-embedding-3-small"),
            api_key=config.get("api_key", ""),
            base_url=config.get("base_url", None),
        )
    elif provider == "local":
        return _create_local_embedder(config)
    else:
        raise ValueError(f"不支持的 Embedding 提供商: {provider}")
