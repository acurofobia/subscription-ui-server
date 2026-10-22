"""Read and validate version 1 of the anonymous UI document."""

from datetime import datetime
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


MAX_DOCUMENT_BYTES = 1024 * 1024
MAX_INTEGER = 9007199254740991
IDENTIFIER = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}')
COMMON_FIELDS = {'id', 'type', 'visible', 'title', 'subtitle', 'text'}
CLIENT_SLOTS = {'block01': 'card', 'block02': 'valueRow', 'block03': 'actionSheet',
                'block04': 'selectionList', 'block05': 'menu', 'block06': 'progress'}
CLIENT_DATETIME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt ][0-9]{2}:[0-9]{2}'
                             r'(?::[0-9]{2}(?:[.,][0-9]+)?)?(?:Z|[+-][0-9]{2}:?[0-9]{2})')


class ValidationError(ValueError):
    """The document does not satisfy the supported contract."""


def _object(value: object, required: set[str], optional: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise ValidationError('Invalid object fields')
    return value


def _array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValidationError('Expected an array')
    return value


def _text(value: object, maximum: int = 4096, minimum: int = 1) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValidationError('Invalid text length or type')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ValidationError('Invalid Unicode text') from None
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValidationError('Invalid identifier')
    return value


def _enum(value: object, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise ValidationError('Unsupported value')
    return value


def _common(record: dict[str, object]) -> None:
    if 'visible' in record and type(record['visible']) is not bool:
        raise ValidationError('Visibility must be boolean')
    for field in ('title', 'subtitle'):
        if field in record:
            _text(record[field])
    if 'text' in record:
        _text(record['text'], minimum=0)


def _url(value: object) -> None:
    text = _text(value, maximum=2048)
    if any(character.isspace() or ord(character) < 32 or 127 <= ord(character) <= 159
           or character == '\\' for character in text):
        raise ValidationError('Invalid URL characters')
    try:
        url = urlsplit(text)
        if (url.scheme != 'https' or not url.hostname or url.username is not None
                or url.password is not None):
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
    except ValueError:
        raise ValidationError('Expected an absolute HTTPS URL without credentials') from None


def _progress(block: dict[str, object]) -> None:
    for field in ('value', 'maximum'):
        value = block[field]
        if type(value) is not int or not 0 <= value <= MAX_INTEGER:
            raise ValidationError('Progress values must be bounded non-negative integers')
    if block['maximum'] == 0 or block['value'] > block['maximum']:
        raise ValidationError('Invalid progress range')
    _enum(block['unit'], ('bytes', 'count', 'percent'))
    if 'period' in block:
        _enum(block['period'], ('day', 'week', 'month', 'total'))
    if block.get('resetAt') is not None:
        try:
            source = _text(block['resetAt'])
            if not CLIENT_DATETIME.fullmatch(source):
                raise ValueError
            date = datetime.fromisoformat(source)
            if date.tzinfo is None or date.utcoffset() is None:
                raise ValueError
        except ValueError:
            raise ValidationError('resetAt must be a datetime with a time zone') from None


class _Validator:
    def __init__(self) -> None:
        self.identifiers: set[str] = set()
        self.block_types: dict[str, str] = {}
        self.page_ids: set[str] = set()
        self.block_references: list[str] = []
        self.page_references: list[str] = []

    def register(self, value: object) -> str:
        identifier = _identifier(value)
        if identifier in self.identifiers:
            raise ValidationError('Duplicate identifier')
        self.identifiers.add(identifier)
        return identifier

    def action(self, value: object) -> None:
        action = _object(value, {'type'}, {'url', 'targetId'})
        kind = _enum(action['type'], ('openExternalUrl', 'openScreen', 'openBlock'))
        if kind == 'openExternalUrl':
            _object(action, {'type', 'url'}, set())
            _url(action['url'])
        else:
            _object(action, {'type', 'targetId'}, set())
            target = _identifier(action['targetId'])
            if kind == 'openBlock':
                self.block_references.append(target)

    def button(self, value: object) -> None:
        button = _object(value, {'id', 'title', 'action'}, {'visible'})
        self.register(button['id'])
        _common(button)
        self.action(button['action'])

    def choice(self, value: object) -> None:
        item = _object(value, {'id', 'title', 'text', 'state', 'buttons'},
                       {'visible', 'stateText', 'detailsPageId'})
        self.register(item['id'])
        _common(item)
        _enum(item['state'], ('available', 'restricted', 'unavailable'))
        if item.get('stateText') is not None:
            _text(item['stateText'])
        if item.get('detailsPageId') is not None:
            self.page_references.append(_identifier(item['detailsPageId']))
        for button in _array(item['buttons']):
            self.button(button)

    def block(self, value: object) -> None:
        block = _object(value, {'id', 'type'}, COMMON_FIELDS | {
            'buttons', 'items', 'value', 'maximum', 'unit', 'period', 'resetAt'})
        kind = _enum(block['type'], ('card', 'valueRow', 'actionSheet', 'selectionList', 'menu', 'progress'))
        required = {'id', 'type'}
        optional = COMMON_FIELDS - required
        if kind in ('card', 'valueRow', 'actionSheet'):
            required |= {'title', 'buttons'}
        elif kind in ('selectionList', 'menu'):
            required.add('items')
        else:
            required |= {'value', 'maximum', 'unit'}
            optional |= {'period', 'resetAt'}
        _object(block, required, optional)
        identifier = self.register(block['id'])
        if identifier in CLIENT_SLOTS and CLIENT_SLOTS[identifier] != kind:
            raise ValidationError('Block type does not match the client slot')
        self.block_types[identifier] = kind
        _common(block)
        if kind in ('card', 'valueRow', 'actionSheet'):
            for button in _array(block['buttons']):
                self.button(button)
        elif kind in ('selectionList', 'menu'):
            for item in _array(block['items']):
                if kind == 'selectionList':
                    self.choice(item)
                else:
                    self.button(item)
        else:
            _progress(block)

    def document(self, value: object) -> dict[str, object]:
        document = _object(value, {'schemaVersion', 'blocks', 'pages'}, set())
        if type(document['schemaVersion']) is not int or document['schemaVersion'] != 1:
            raise ValidationError('Unsupported schema version')
        for block in _array(document['blocks']):
            self.block(block)
        for value in _array(document['pages']):
            page = _object(value, {'id', 'title', 'text'}, set())
            self.page_ids.add(self.register(page['id']))
            _text(page['title'])
            _text(page['text'], maximum=32768, minimum=0)
        if any(self.block_types.get(target) != 'actionSheet' for target in self.block_references):
            raise ValidationError('openBlock must reference an existing actionSheet')
        if any(target not in self.page_ids for target in self.page_references):
            raise ValidationError('Unknown details page')
        return document


def validate_document(document: object) -> dict[str, object]:
    """Validate a full snapshot without adding defaults or changing its content."""
    return _Validator().document(document)


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError('Duplicate JSON key')
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValidationError('Non-finite JSON numbers are not allowed')


def load_document(path: Path) -> dict[str, object]:
    """Read at most 1 MiB and validate strict UTF-8 JSON from disk."""
    with path.open('rb') as source:
        payload = source.read(MAX_DOCUMENT_BYTES + 1)
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise ValidationError('Document exceeds 1 MiB')
    try:
        document = json.loads(payload.decode('utf-8'), object_pairs_hook=_unique_pairs,
                              parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise ValidationError('Invalid UTF-8 JSON document') from None
    return validate_document(document)
