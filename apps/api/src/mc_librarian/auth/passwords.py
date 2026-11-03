"""密码哈希：argon2id（argon2-cffi 的 RFC 9106 默认参数），库里只存哈希。"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()

# 用户不存在时也拿它跑一次校验，抹平「查不到用户」与「密码错」的时序差。
_DUMMY_HASH = _hasher.hash("mc-librarian-dummy-password-for-timing")


def hash_password(password: str) -> str:
    """生成 argon2id 哈希（含随机 salt）。"""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码；哈希损坏或算法不认识都按「不匹配」处理，不向上抛。"""
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def burn_verification(password: str) -> None:
    """对不存在的用户跑一次等价开销的校验，避免用响应时间探测用户名是否存在。"""
    verify_password(password, _DUMMY_HASH)
