"""认证与配置的单测：密码哈希、令牌校验、生产环境硬约束。

这几条都是"错了会很难发现"的地方——比如 JWT 密钥退回默认值，
本地跑一切正常，直到有人拿示例密钥伪造了一个管理员令牌。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import Settings, get_settings
from app.core.security import create_access_token, decode_token, hash_password, verify_password


# ---------------------------- 密码 ----------------------------
def test_password_roundtrip() -> None:
    stored = hash_password("Jyweu7pl!")
    assert stored.startswith("pbkdf2_sha256$260000$")
    assert verify_password("Jyweu7pl!", stored) is True


def test_wrong_password_rejected() -> None:
    stored = hash_password("Jyweu7pl!")
    assert verify_password("jyweu7pl!", stored) is False


def test_malformed_hash_rejected_without_crash() -> None:
    """库里的哈希被人手工改坏时，应当返回 False，而不是抛栈打挂登录接口。"""
    assert verify_password("anything", "not-a-valid-hash") is False
    assert verify_password("anything", "bcrypt$10$abc$def") is False


def test_same_password_different_salt() -> None:
    assert hash_password("same-pass") != hash_password("same-pass")


# ---------------------------- 令牌 ----------------------------
def test_token_roundtrip() -> None:
    payload = decode_token(create_access_token(7, "staff"))
    assert payload["sub"] == "7"
    assert payload["role"] == "staff"


def test_expired_token_rejected() -> None:
    s = get_settings()
    expired = jwt.encode(
        {
            "sub": "1",
            "role": "admin",
            "exp": datetime.now(UTC) - timedelta(minutes=1),
        },
        s.jwt_secret,
        algorithm=s.jwt_algorithm,
    )
    with pytest.raises(jwt.PyJWTError):
        decode_token(expired)


def test_tampered_token_rejected() -> None:
    token = create_access_token(1, "staff")
    with pytest.raises(jwt.PyJWTError):
        decode_token(token[:-4] + "AAAA")


def test_token_signed_with_other_secret_rejected() -> None:
    forged = jwt.encode(
        {"sub": "1", "role": "admin", "exp": datetime.now(UTC) + timedelta(hours=1)},
        "attacker-secret-" + "x" * 24,  # 长度合规，但不是本系统的密钥
        algorithm="HS256",
    )
    with pytest.raises(jwt.PyJWTError):
        decode_token(forged)


# ---------------------------- 生产环境自检 ----------------------------
SAFE = {
    "app_env": "prod",
    "jwt_secret": "a" * 64,
    "access_token_expire_minutes": 120,
    "llm_api_key": "sk-test",
}


def test_prod_with_safe_config_has_no_problem() -> None:
    assert Settings(**SAFE).production_problems() == []


def test_prod_rejects_default_secret() -> None:
    problems = Settings(**{**SAFE, "jwt_secret": "dev-secret-change-me"}).production_problems()
    assert any("默认值" in p for p in problems)


def test_prod_rejects_short_secret() -> None:
    problems = Settings(**{**SAFE, "jwt_secret": "short"}).production_problems()
    assert any("长度不足" in p for p in problems)


def test_prod_rejects_long_token_ttl() -> None:
    problems = Settings(**{**SAFE, "access_token_expire_minutes": 720}).production_problems()
    assert any("240" in p for p in problems)


def test_prod_requires_llm_key() -> None:
    problems = Settings(**{**SAFE, "llm_api_key": ""}).production_problems()
    assert any("LLM_API_KEY" in p for p in problems)


def test_dev_env_does_not_block_defaults() -> None:
    """本地开发用默认配置不该被拦（否则新人第一天就跑不起来）。"""
    settings = Settings(app_env="dev")
    assert settings.is_prod is False
