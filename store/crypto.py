"""对检视人的 gho_ token 做对称加解密（Fernet）。"""
from cryptography.fernet import Fernet, InvalidToken


class TokenCipher:
    def __init__(self, key: str):
        if not key:
            raise ValueError(
                "FERNET_KEY 未配置。生成命令："
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        self._f = Fernet(key.encode() if isinstance(key, str) else key)

    def encrypt(self, plaintext: str) -> bytes:
        return self._f.encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        try:
            return self._f.decrypt(token).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("token 解密失败：FERNET_KEY 是否与写入时一致？") from exc
