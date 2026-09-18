"""Safe product display master loader.

This module intentionally avoids compressed embedded payloads so a damaged
classification file can never prevent the Flask app from starting.
"""

def lookup(icube, fallback_name="", fallback_spec=""):
    code = str(icube or "").strip().upper()
    return {
        "name": fallback_name or "제품명 미등록",
        "type": "마스터 복구중",
        "size": fallback_spec or "-",
        "category": "확인 필요",
        "mapped": False,
        "icube": code,
    }
