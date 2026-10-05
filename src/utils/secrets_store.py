# -*- coding: utf-8 -*-
"""敏感配置加密：API Key 用 Windows DPAPI 加密后落盘（NFR-06）

**为什么不能明文存？**
`config/settings.json` 里存着用户的 LLM / 云端 ASR 密钥。原实现明文写入，
任何拿到该文件的人（同步盘、备份、误发给他人）都能直接读到密钥。

**为什么用 DPAPI 而不是自己写加密？**
- DPAPI（CryptProtectData）是 Windows 原生方案，**密钥由系统账户保管**，
  密文只能被**同一台机器上的同一用户**解密——不需要我们自己管理任何密钥文件，
  也不会引入"加密密钥存哪"这个无解的问题。
- 跨平台/非 Windows 环境自动降级为明文 + 显式告警（保证开发/测试不受阻）。

存储格式：`"enc:v1:<base64密文>"`，与旧版明文**自动兼容**——
读到明文就用明文，保存时自动升级为密文（懒迁移）。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import sys
from typing import Optional

from src.utils.logger import logger

ENC_PREFIX = "enc:v1:"


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[DataBlob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _protect(data: bytes, entropy: bytes) -> Optional[bytes]:
    """DPAPI 加密（CRYPTPROTECT_UI_FORBIDDEN）"""
    if sys.platform != "win32":
        return None
    try:
        in_blob, in_buf = _blob(data)
        ent_blob, ent_buf = _blob(entropy)
        out_blob = DataBlob()
        ok = ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(in_blob), None, ctypes.byref(ent_blob), None, None,
            0x01, ctypes.byref(out_blob))          # 0x01 = UI_FORBIDDEN
        if not ok:
            logger.warning("DPAPI 加密失败，密钥将以明文存储")
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    except Exception:  # noqa: BLE001
        logger.warning("DPAPI 调用异常，密钥将以明文存储", exc_info=True)
        return None


def _unprotect(data: bytes, entropy: bytes) -> Optional[bytes]:
    """DPAPI 解密"""
    if sys.platform != "win32":
        return None
    try:
        in_blob, in_buf = _blob(data)
        ent_blob, ent_buf = _blob(entropy)
        out_blob = DataBlob()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), None, ctypes.byref(ent_blob), None, None,
            0x01, ctypes.byref(out_blob))
        if not ok:
            logger.warning("DPAPI 解密失败（可能是文件被复制到其他机器/用户）")
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(out_blob.pbData)
    except Exception:  # noqa: BLE001
        logger.warning("DPAPI 解密异常", exc_info=True)
        return None


def encrypt_secret(plain: str, salt: str = "lessonrecorder.v1") -> str:
    """加密密钥字符串；失败时返回原文（保证不丢配置）"""
    if not plain:
        return ""
    if plain.startswith(ENC_PREFIX):
        return plain                      # 已加密，幂等
    blob = _protect(plain.encode("utf-8"), salt.encode("utf-8"))
    if blob is None:
        return plain
    return ENC_PREFIX + base64.b64encode(blob).decode("ascii")


def decrypt_secret(stored: str, salt: str = "lessonrecorder.v1") -> str:
    """解密密钥字符串；非密文或解密失败时返回原文（兼容旧配置）"""
    if not stored:
        return ""
    if not stored.startswith(ENC_PREFIX):
        return stored                      # 旧版明文，直接用（懒迁移）
    try:
        raw = base64.b64decode(stored[len(ENC_PREFIX):])
    except Exception:  # noqa: BLE001
        logger.warning("密文格式损坏，按原文处理")
        return stored
    plain = _unprotect(raw, salt.encode("utf-8"))
    if plain is None:
        # 解不开不能静默返回密文（会让用户以为 Key 配好了却一直认证失败）
        logger.error("密钥解密失败，请在设置中重新填写 API Key")
        return ""
    return plain.decode("utf-8", errors="replace")


def is_encrypted(stored: str) -> bool:
    return bool(stored) and stored.startswith(ENC_PREFIX)


def encryption_supported() -> bool:
    """当前平台是否支持 DPAPI 加密"""
    if sys.platform != "win32":
        return False
    probe = _protect(b"probe", b"lessonrecorder.v1")
    return probe is not None