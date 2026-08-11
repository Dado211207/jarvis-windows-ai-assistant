"""Is this .exe really from who it claims to be?

JARVIS downloads one executable it did not write — Ollama's own Windows
installer — and then runs it. Running a downloaded installer is the
single most dangerous thing in this product, so the check before it is
not "did the download finish" but the two questions Windows itself asks:

  1. Does the file carry a valid Authenticode signature that chains to a
     certificate authority this machine trusts, and has the file not
     been modified since it was signed? (`WinVerifyTrust`)
  2. Who signed it? (the certificate's subject name)

Both are needed. A valid signature alone only proves *somebody* signed
it, and anybody can buy a code-signing certificate. The name alone
proves nothing without the signature. Together they mean: this file was
produced by that publisher and has not been altered since.

**No subprocess and no shell.** This talks to wintrust.dll and crypt32.dll
directly through ctypes, so there is no command line for a path to be
smuggled into — CLAUDE.md's Safety rules on subprocess use.

Everything here returns a verdict rather than raising, and reports
"could not verify" as a failure rather than as a pass. On a machine
where the check cannot run, the safe reading of "unknown" is "no".
"""

import sys
from dataclasses import dataclass
from typing import Optional

from app.logging_config import get_logger

logger = get_logger("core.authenticode")


@dataclass(frozen=True)
class SignatureVerdict:
    """The answer, and enough detail to tell a user what went wrong."""

    trusted: bool
    signer: str
    detail: str

    def is_from(self, publisher: str) -> bool:
        """Signed, trusted, *and* by the expected publisher."""
        return self.trusted and publisher.lower() in self.signer.lower()


def verify(path, expected_publisher: str = "") -> SignatureVerdict:
    """Verify *path*'s Authenticode signature.

    When *expected_publisher* is given, the verdict is only `trusted`
    if the signing certificate's subject contains it.
    """
    if sys.platform != "win32":
        return SignatureVerdict(
            trusted=False,
            signer="",
            detail="Code signatures can only be verified on Windows.",
        )

    trusted, detail = _verify_trust(str(path))
    signer = _signer_name(str(path)) if trusted else ""
    if not trusted:
        return SignatureVerdict(trusted=False, signer=signer, detail=detail)

    if expected_publisher and expected_publisher.lower() not in signer.lower():
        return SignatureVerdict(
            trusted=False,
            signer=signer,
            detail=(
                f"The file is signed, but by “{signer or 'an unknown publisher'}” rather "
                f"than {expected_publisher}. JARVIS will not run it."
            ),
        )

    return SignatureVerdict(
        trusted=True,
        signer=signer,
        detail=f"Signed by {signer}, and the signature is valid on this machine.",
    )


# ---------------------------------------------------------------------------
# WinVerifyTrust
# ---------------------------------------------------------------------------

_WTD_UI_NONE = 2
_WTD_REVOKE_WHOLECHAIN = 1
_WTD_CHOICE_FILE = 1
_WTD_STATEACTION_VERIFY = 1
_WTD_STATEACTION_CLOSE = 2
_WTD_SAFER_FLAG = 0x100

# Winerror values WinVerifyTrust returns as its own result, each of which
# is a different problem for the user.
_TRUST_ERRORS = {
    0x800B0100: "The file is not signed at all.",
    0x800B0101: "The signing certificate has expired.",
    0x800B0109: "The signature chains to a certificate this machine does not trust.",
    0x80092010: "The signing certificate has been revoked.",
    0x800B0004: "The file has been modified since it was signed.",
    0x80096010: "The file has been modified since it was signed.",
}


def _verify_trust(path: str):
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8),
        ]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR),
            ("hFile", wintypes.HANDLE), ("pgKnownSubject", ctypes.c_void_p),
        ]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [
            ("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
            ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
            ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
            ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)), ("dwStateAction", wintypes.DWORD),
            ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPCWSTR),
            ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
            ("pSignatureSettings", ctypes.c_void_p),
        ]

    # WINTRUST_ACTION_GENERIC_VERIFY_V2
    action = GUID(0xAAC56B, 0xCD44, 0x11D0, (0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))

    file_info = WINTRUST_FILE_INFO(
        cbStruct=ctypes.sizeof(WINTRUST_FILE_INFO), pcwszFilePath=path,
        hFile=None, pgKnownSubject=None,
    )
    data = WINTRUST_DATA(
        cbStruct=ctypes.sizeof(WINTRUST_DATA), pPolicyCallbackData=None, pSIPClientData=None,
        dwUIChoice=_WTD_UI_NONE, fdwRevocationChecks=_WTD_REVOKE_WHOLECHAIN,
        dwUnionChoice=_WTD_CHOICE_FILE, pFile=ctypes.pointer(file_info),
        dwStateAction=_WTD_STATEACTION_VERIFY, hWVTStateData=None, pwszURLReference=None,
        dwProvFlags=_WTD_SAFER_FLAG, dwUIContext=0, pSignatureSettings=None,
    )

    try:
        wintrust = ctypes.WinDLL("wintrust.dll")
        wintrust.WinVerifyTrust.restype = ctypes.c_long
        result = wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
        data.dwStateAction = _WTD_STATEACTION_CLOSE
        wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    except Exception as exc:  # noqa: BLE001
        logger.warning("WinVerifyTrust could not be called: %s", exc)
        return False, "The signature could not be checked on this machine."

    if result == 0:
        return True, "The signature is valid."

    code = result & 0xFFFFFFFF
    return False, _TRUST_ERRORS.get(code, f"The signature is not valid (0x{code:08X}).")


# ---------------------------------------------------------------------------
# Who signed it
# ---------------------------------------------------------------------------

_CERT_QUERY_OBJECT_FILE = 1
_CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED = 1 << 10
_CERT_QUERY_FORMAT_FLAG_BINARY = 1 << 1
_CMSG_SIGNER_INFO_PARAM = 6
_CERT_FIND_SUBJECT_CERT = 0x000B0000
_X509_ASN_ENCODING = 0x00000001
_PKCS_7_ASN_ENCODING = 0x00010000
_CERT_NAME_SIMPLE_DISPLAY_TYPE = 4


def _signer_name(path: str) -> str:
    """The signing certificate's display name, or "".

    Only ever used for display and for the publisher comparison — the
    trust decision itself is WinVerifyTrust's, above.
    """
    import ctypes
    from ctypes import wintypes

    class CRYPT_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]

    class CRYPT_ALGORITHM_IDENTIFIER(ctypes.Structure):
        _fields_ = [("pszObjId", ctypes.c_char_p), ("Parameters", CRYPT_BLOB)]

    class CRYPT_BIT_BLOB(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
            ("cUnusedBits", wintypes.DWORD),
        ]

    class CERT_PUBLIC_KEY_INFO(ctypes.Structure):
        _fields_ = [("Algorithm", CRYPT_ALGORITHM_IDENTIFIER), ("PublicKey", CRYPT_BIT_BLOB)]

    class CRYPT_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("cAttr", wintypes.DWORD), ("rgAttr", ctypes.c_void_p)]

    class CMSG_SIGNER_INFO(ctypes.Structure):
        _fields_ = [
            ("dwVersion", wintypes.DWORD), ("Issuer", CRYPT_BLOB), ("SerialNumber", CRYPT_BLOB),
            ("HashAlgorithm", CRYPT_ALGORITHM_IDENTIFIER),
            ("HashEncryptionAlgorithm", CRYPT_ALGORITHM_IDENTIFIER),
            ("EncryptedHash", CRYPT_BLOB),
            ("AuthAttrs", CRYPT_ATTRIBUTES), ("UnauthAttrs", CRYPT_ATTRIBUTES),
        ]

    class CERT_INFO(ctypes.Structure):
        _fields_ = [
            ("dwVersion", wintypes.DWORD), ("SerialNumber", CRYPT_BLOB),
            ("SignatureAlgorithm", CRYPT_ALGORITHM_IDENTIFIER), ("Issuer", CRYPT_BLOB),
            ("NotBefore", wintypes.FILETIME), ("NotAfter", wintypes.FILETIME),
            ("Subject", CRYPT_BLOB), ("SubjectPublicKeyInfo", CERT_PUBLIC_KEY_INFO),
            ("IssuerUniqueId", CRYPT_BIT_BLOB), ("SubjectUniqueId", CRYPT_BIT_BLOB),
            ("cExtension", wintypes.DWORD), ("rgExtension", ctypes.c_void_p),
        ]

    class CERT_CONTEXT(ctypes.Structure):
        _fields_ = [
            ("dwCertEncodingType", wintypes.DWORD), ("pbCertEncoded", ctypes.POINTER(ctypes.c_ubyte)),
            ("cbCertEncoded", wintypes.DWORD), ("pCertInfo", ctypes.POINTER(CERT_INFO)),
            ("hCertStore", ctypes.c_void_p),
        ]

    try:
        crypt32 = ctypes.WinDLL("crypt32.dll")
        store = ctypes.c_void_p()
        message = ctypes.c_void_p()
        ok = crypt32.CryptQueryObject(
            _CERT_QUERY_OBJECT_FILE, ctypes.c_wchar_p(path),
            _CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED, _CERT_QUERY_FORMAT_FLAG_BINARY,
            0, None, None, None, ctypes.byref(store), ctypes.byref(message), None,
        )
        if not ok:
            return ""

        size = wintypes.DWORD(0)
        if not crypt32.CryptMsgGetParam(message, _CMSG_SIGNER_INFO_PARAM, 0, None, ctypes.byref(size)):
            return ""
        buffer = ctypes.create_string_buffer(size.value)
        if not crypt32.CryptMsgGetParam(
            message, _CMSG_SIGNER_INFO_PARAM, 0, buffer, ctypes.byref(size)
        ):
            return ""

        signer = ctypes.cast(buffer, ctypes.POINTER(CMSG_SIGNER_INFO)).contents
        wanted = CERT_INFO()
        wanted.Issuer = signer.Issuer
        wanted.SerialNumber = signer.SerialNumber

        crypt32.CertFindCertificateInStore.restype = ctypes.POINTER(CERT_CONTEXT)
        certificate = crypt32.CertFindCertificateInStore(
            store, _X509_ASN_ENCODING | _PKCS_7_ASN_ENCODING, 0,
            _CERT_FIND_SUBJECT_CERT, ctypes.byref(wanted), None,
        )
        if not certificate:
            return ""

        length = crypt32.CertGetNameStringW(
            certificate, _CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, None, None, 0,
        )
        if length <= 1:
            return ""
        name = ctypes.create_unicode_buffer(length)
        crypt32.CertGetNameStringW(
            certificate, _CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, None, name, length,
        )
        crypt32.CertFreeCertificateContext(certificate)
        return name.value.strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read the signing certificate: %s", exc)
        return ""


def sha256(path, chunk_size: int = 1024 * 1024) -> Optional[str]:
    """The file's SHA-256, or None if it could not be read.

    Shown to the user next to the signature verdict. It is not a
    verification on its own — nothing here has an expected value to
    compare it against, because Ollama's installer is a moving target —
    but it is what somebody needs to check the file independently.
    """
    import hashlib
    from pathlib import Path

    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(chunk_size), b""):
                digest.update(chunk)
    except OSError:
        logger.debug("Could not hash %s", path, exc_info=True)
        return None
    return digest.hexdigest()
