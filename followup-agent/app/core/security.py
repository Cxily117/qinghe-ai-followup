"""安全模块：密码哈希（PBKDF2）+ JWT 签发/校验。

为什么不用 passlib[bcrypt]：bcrypt 4.x 与 passlib 存在兼容告警，
PBKDF2-HMAC-SHA256 属于 NIST 推荐方案、零额外依赖，骨架阶段足够且稳定。
生产如要求更强，可无缝替换为 argon2。
"""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import get_settings

_ITERATIONS = 260_000  # PBKDF2 迭代次数（OWASP 2023 建议 >=600k 时可调高）


# ---------------------------- 密码 ----------------------------
def hash_password(password: str) -> str:
    """返回 `pbkdf2_sha256$迭代数$盐$哈希`，盐随机、格式自描述便于以后升级算法。"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常数时间比较，防时序攻击。"""
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(dk.hex(), digest)


# ---------------------------- JWT ----------------------------
def create_access_token(user_id: int, role: str) -> str:
    s = get_settings()
    payload = {
        "sub": str(user_id),          # 标准声明：主体 = 用户 ID
        "role": role,                 # 冗余角色，便于前端控制菜单（鉴权仍以库为准）
        "exp": datetime.now(timezone.utc) + timedelta(minutes=s.access_token_expire_minutes),
    }
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_token(token: str) -> dict:
    """校验签名与过期时间，失败抛 jwt.PyJWTError（由依赖层统一转 401）。"""
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
