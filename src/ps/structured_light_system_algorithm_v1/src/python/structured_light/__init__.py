"""P0 offline-only contracts. Importing this package never touches hardware."""
__all__ = ["ConfigError", "SystemConfig", "load_config", "validate_config", "Profile", "ProfileError", "ProfilePoint", "decode_profile", "encode_profile", "CheckResult", "check_contracts", "verify_baselines"]


def __getattr__(name):
    if name in {"ConfigError", "SystemConfig", "load_config", "validate_config"}:
        from . import config
        return getattr(config, name)
    if name in {"Profile", "ProfileError", "ProfilePoint", "decode_profile", "encode_profile"}:
        from . import profile
        return getattr(profile, name)
    if name in {"CheckResult", "check_contracts", "verify_baselines"}:
        from . import checks
        return getattr(checks, name)
    raise AttributeError(name)
