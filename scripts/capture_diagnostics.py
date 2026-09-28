"""Bounded diagnostics that never include request URLs or credentials."""
import re


def exception_details(exc, secrets=()):
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen and len(chain) < 5:
        seen.add(id(exc))
        message = str(exc)
        for secret in secrets:
            if secret:
                message = message.replace(secret, "[redacted]")
        message = re.sub(r"https?://[^\s'\"<>]+", "[url]", message)
        message = re.sub(r"(?i)(bearer\s+)[^\s,'\"]+", r"\1[redacted]", message)
        message = re.sub(r"(?i)((?:sessionid|token|authorization|api_key)\s*[=:]\s*)[^\s&,'\")]+",
                         r"\1[redacted]", message)
        chain.append({"type": type(exc).__name__, "message": message[:400]})
        nested = next((arg for arg in exc.args if isinstance(arg, BaseException)), None)
        exc = exc.__cause__ or exc.__context__ or nested
    return chain
