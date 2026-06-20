import hashlib
import secrets
import time
from typing import Dict

# 記憶體中的 active_tokens，儲存 token -> 權杖過期時間戳記 (float)
active_tokens: Dict[str, float] = {}

# Token 的存活時間為 24 小時（單位：秒）
TOKEN_EXPIRE_SECONDS = 86400

def hash_password(password: str, salt: str = None) -> tuple:
    """使用 PBKDF2_HMAC SHA-256 演算法對密碼進行加鹽雜湊"""
    if not salt:
        salt = secrets.token_hex(16)
    pw_hash = hashlib.pbkdf2_hmac(
        'sha256',
        password.encode('utf-8'),
        salt.encode('utf-8'),
        100000
    ).hex()
    return pw_hash, salt

def verify_password(password: str, pw_hash: str, salt: str) -> bool:
    """驗證輸入的密碼與雜湊是否相符"""
    test_hash, _ = hash_password(password, salt)
    return test_hash == pw_hash

def generate_token() -> str:
    """生成一個隨機且安全的安全權杖 Token"""
    token = secrets.token_hex(32)
    # 設定過期時間為當前時間加上 Token 存活秒數
    active_tokens[token] = time.time() + TOKEN_EXPIRE_SECONDS
    return token

def check_token(token: str) -> bool:
    """檢查 Token 是否存在且尚未過期"""
    if not token:
        return False
    current_time = time.time()
    expiry = active_tokens.get(token, 0.0)
    if current_time > expiry:
        # 已過期，自記憶體中移除
        active_tokens.pop(token, None)
        return False
    return True

def revoke_token(token: str):
    """註銷（移除）指定的 Token"""
    active_tokens.pop(token, None)
