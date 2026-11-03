"""令牌哈希：明文 token 落库前统一做 sha256（hex）。

会话 cookie 与个人访问令牌（PAT）共用它——控制面库里永远只有哈希，
明文只出现在下发给调用方的那一刻。
"""

from __future__ import annotations

import hashlib


def hash_token(raw_token: str) -> str:
    """明文 token → sha256（hex）。"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
