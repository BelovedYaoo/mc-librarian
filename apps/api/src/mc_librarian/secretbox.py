"""`settings` 表里密钥值的加密（issue #11）：AES-GCM，主密钥来自 `MC_LIBRARIAN_SECRET`。

**为什么必须显式配主密钥**：自动生成并落盘看起来更省事，但数据根迁移/重装后密钥丢了，
库里已有的密文就再也解不开，而且失败点在**解密时**（一次问答或摘要任务），比启动时晚得多。
所以缺失或过短时服务直接启动失败（`main.lifespan` 调 `require_secret`），错误里给出怎么设。

密文格式：`base64(nonce ‖ ciphertext ‖ tag)`。nonce 每次加密随机取 12 字节，AES-GCM 的
nonce 长度固定，因此不用把长度写进密文；密钥是主密钥的 sha256（32 字节 → AES-256-GCM）。

本模块不 import 仓库内其它模块：`Settings.secret` 由调用方传进来，避免
`config → settings_store → secretbox → config` 的模块级循环。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# AES-GCM 的推荐 nonce 长度。
NONCE_BYTES: Final = 12

# 主密钥的最小长度。sha256 把任意长度映射成 32 字节密钥，但太短的主密钥可暴力枚举，
# 这里只做下限护栏（不校验字符集：运维可能用 openssl rand -base64 的串）。
MIN_SECRET_LENGTH: Final = 16


class SecretError(RuntimeError):
    """主密钥缺失 / 过短，或密文解不开（主密钥换了、密文被改坏）。"""


def require_secret(secret: str | None) -> str:
    """取主密钥；缺失或过短抛 `SecretError`（消息里给出可操作的设置方式）。"""
    value = (secret or "").strip()

    if len(value) < MIN_SECRET_LENGTH:
        raise SecretError(
            "缺少主密钥 MC_LIBRARIAN_SECRET（至少 "
            f"{MIN_SECRET_LENGTH} 个字符）：`settings` 表里的密钥"
            "（`llm.api_key` / `alerts.webhook_url`）用 AES-GCM 加密存储，没有主密钥无法读写。"
            " 例：MC_LIBRARIAN_SECRET=$(openssl rand -hex 32)。"
            " 不要在服务里临时生成：重启后旧密文就解不开了。"
        )

    return value


def derive_key(secret: str) -> bytes:
    """主密钥 → 32 字节 AES 密钥。"""
    return hashlib.sha256(secret.encode("utf-8")).digest()


def encrypt(secret: str, plaintext: str) -> str:
    """明文 → `base64(nonce ‖ ciphertext ‖ tag)`。"""
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(derive_key(secret)).encrypt(nonce, plaintext.encode("utf-8"), None)

    return base64.b64encode(nonce + sealed).decode("ascii")


def decrypt(secret: str, token: str) -> str:
    """密文 → 明文；解不开（换了主密钥 / 密文被改）抛 `SecretError`，不静默返回空串。"""
    try:
        raw = base64.b64decode(token, validate=True)
        nonce, sealed = raw[:NONCE_BYTES], raw[NONCE_BYTES:]

        return AESGCM(derive_key(secret)).decrypt(nonce, sealed, None).decode("utf-8")
    except (InvalidTag, binascii.Error, ValueError) as error:
        raise SecretError(
            "无法解密已存储的密钥：主密钥（MC_LIBRARIAN_SECRET）与写入时不一致，或密文被改坏"
        ) from error
