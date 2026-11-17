"""File-backed decisions for the application's device-access-v1 protocol."""
from pathlib import Path
import re

from provisioning import load_json


HWID_PATTERN = re.compile(r'[A-Za-z0-9=-]{10,64}')
STATES = ('allowed', 'not_allowed', 'device_revoked', 'device_limit_reached')


def _decision(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.keys() != {'state', 'revision'}:
        raise ValueError('Invalid device decision')
    revision = value['revision']
    if (value['state'] not in STATES or type(revision) is not int
            or not 0 <= revision <= 2**63 - 1):
        raise ValueError('Invalid device decision')
    return value


def load_policy(path: Path) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
    document = load_json(path, 1024 * 1024)
    if (not isinstance(document, dict) or document.keys() != {'schemaVersion', 'default', 'devices'}
            or type(document['schemaVersion']) is not int or document['schemaVersion'] != 1
            or not isinstance(document['devices'], dict)):
        raise ValueError('Invalid device policy')
    default = _decision(document['default'])
    devices = {}
    for hwid, value in document['devices'].items():
        if HWID_PATTERN.fullmatch(hwid) is None:
            raise ValueError('Invalid device policy')
        devices[hwid] = _decision(value)
    return default, devices


def decision_for(path: Path, hwid: str) -> dict[str, object]:
    default, devices = load_policy(path)
    return {'schemaVersion': 1, 'hwid': hwid, **devices.get(hwid, default)}
