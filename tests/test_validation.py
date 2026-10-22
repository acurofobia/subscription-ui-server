import copy
import json
from pathlib import Path
import tempfile
import unittest

from validation import ValidationError, load_document, validate_document


SAMPLE = Path(__file__).resolve().parents[1] / 'data' / 'subscription.example.json'


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.document = json.loads(SAMPLE.read_text(encoding='utf-8'))

    def test_sample_preserves_content_order_and_internal_actions(self):
        result = load_document(SAMPLE)
        self.assertEqual(result, self.document)
        self.assertEqual([block['id'] for block in result['blocks']],
                         ['block01', 'block02', 'block03', 'block04', 'block05', 'block06'])
        self.assertEqual(result['blocks'][4]['items'][0]['action'],
                         {'type': 'openScreen', 'targetId': 'screen01'})
        self.assertEqual(result['blocks'][4]['items'][1]['action'],
                         {'type': 'openBlock', 'targetId': 'block03'})

    def test_content_is_independent_of_ids(self):
        self.document['blocks'][1]['title'] = '1200 бонусов'
        self.document['blocks'][1]['subtitle'] = 'Доступно'
        self.assertEqual(validate_document(self.document)['blocks'][1]['id'], 'block02')

    def test_reserved_slot_types_match_the_current_client(self):
        self.document['blocks'][0]['type'] = 'valueRow'
        with self.assertRaises(ValidationError):
            validate_document(self.document)
        self.document['blocks'][0]['id'] = 'customValue'
        validate_document(self.document)

    def test_empty_lists_and_optional_visibility_are_valid(self):
        self.assertEqual(validate_document({'schemaVersion': 1, 'blocks': [], 'pages': []}),
                         {'schemaVersion': 1, 'blocks': [], 'pages': []})
        for block in self.document['blocks']:
            block.pop('visible', None)
        validate_document(self.document)

    def test_missing_unknown_and_wrong_root_fields(self):
        for document in [None, [], {}, {'schemaVersion': 1, 'blocks': []},
                         dict(self.document, unexpected=True),
                         dict(self.document, blocks={}), dict(self.document, pages={})]:
            with self.subTest(document_type=type(document).__name__):
                with self.assertRaises(ValidationError):
                    validate_document(document)
        for version in [True, 1.0, 0, 2, '1', None]:
            with self.subTest(version=version), self.assertRaises(ValidationError):
                validate_document(dict(self.document, schemaVersion=version))

    def test_invalid_block_fields(self):
        for field, value in [('type', 'custom'), ('id', '../block'), ('visible', 'false'),
                             ('visible', None), ('title', ''), ('title', 'x' * 4097),
                             ('text', 'x' * 4097), ('buttons', {}), ('surprise', 1)]:
            document = copy.deepcopy(self.document)
            document['blocks'][0][field] = value
            with self.subTest(field=field, value_type=type(value).__name__):
                with self.assertRaises(ValidationError):
                    validate_document(document)
        for field in ['id', 'type', 'title', 'buttons']:
            document = copy.deepcopy(self.document)
            del document['blocks'][0][field]
            with self.subTest(missing=field), self.assertRaises(ValidationError):
                validate_document(document)

    def test_ids_are_globally_unique_and_valid(self):
        for identifier in ['', '1block', 'a' * 65, 'block 01', 'блок01']:
            self.document['blocks'][0]['id'] = identifier
            with self.subTest(identifier=identifier), self.assertRaises(ValidationError):
                validate_document(self.document)
        self.setUp()
        self.document['pages'][0]['id'] = 'button01'
        with self.assertRaises(ValidationError):
            validate_document(self.document)

    def test_hidden_elements_still_require_valid_references(self):
        self.document['blocks'][4]['visible'] = False
        action = self.document['blocks'][4]['items'][1]['action']
        for target in ['missing', 'block01', 'page01', None]:
            action['targetId'] = target
            with self.subTest(target=target), self.assertRaises(ValidationError):
                validate_document(self.document)

    def test_page_references_are_resolved_after_all_pages(self):
        item = self.document['blocks'][3]['items'][0]
        item['detailsPageId'] = 'missing'
        with self.assertRaises(ValidationError):
            validate_document(self.document)
        item['detailsPageId'] = None
        validate_document(self.document)

    def test_choice_states_buttons_and_page_fields(self):
        for field, value in [('state', 'planRequired'), ('state', True),
                             ('stateText', ''), ('stateText', 1), ('detailsPageId', ''),
                             ('buttons', None), ('visible', 0)]:
            document = copy.deepcopy(self.document)
            document['blocks'][3]['items'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_document(document)
        for field, value in [('title', ''), ('text', 'x' * 32769), ('extra', True)]:
            document = copy.deepcopy(self.document)
            document['pages'][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validate_document(document)

    def test_actions_require_exact_fields_and_known_types(self):
        actions = [None, {}, {'type': 'runScript', 'script': 'x'},
                   {'type': 'openScreen'}, {'type': 'openBlock', 'targetId': ''},
                   {'type': 'openScreen', 'targetId': 'screen01', 'url': 'https://example.com'},
                   {'type': 'openExternalUrl', 'url': 'https://example.com', 'targetId': 'x'}]
        for action in actions:
            self.document['blocks'][0]['buttons'][0]['action'] = action
            with self.subTest(action=action), self.assertRaises(ValidationError):
                validate_document(self.document)

    def test_external_urls_are_https_and_well_formed(self):
        urls = ['http://example.com', 'javascript:alert(1)', '/local', 'https://',
                'https://user:password@example.com', 'https://example.com:bad',
                'https://example.com:65536', 'https://example.com/a b',
                'https://example.com/\npath', 'https://example.com\\@other.com',
                'https://example.com/' + 'x' * 2048]
        for url in urls:
            self.document['blocks'][0]['buttons'][0]['action']['url'] = url
            with self.subTest(url_index=urls.index(url)), self.assertRaises(ValidationError):
                validate_document(self.document)

    def test_progress_requires_bounded_integer_values(self):
        for field, value in [('value', -1), ('value', True), ('value', 1.5),
                             ('value', 2000000001), ('maximum', 0),
                             ('maximum', 9007199254740992), ('unit', 'rubles'),
                             ('period', 'hour'), ('resetAt', '2026-10-18'),
                             ('resetAt', '2026-10-18T12:00:00')]:
            document = copy.deepcopy(self.document)
            document['blocks'][5][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                validate_document(document)
        for date in [None, '2026-10-18T12:00:00Z', '2026-10-18T17:00:00+05:00']:
            self.document['blocks'][5]['resetAt'] = date
            validate_document(self.document)

    def test_reset_dates_match_the_current_clients_supported_format(self):
        for date in ('20261022T120000Z', '2026-10-22T12:00:00+03:00:30'):
            self.document['blocks'][5]['resetAt'] = date
            with self.subTest(date=date), self.assertRaises(ValidationError):
                validate_document(self.document)

    def test_loader_rejects_invalid_json_encoding_constants_and_duplicate_keys(self):
        payloads = [b'not json', b'[]', b'\xff', b'{"schemaVersion":1,"schemaVersion":1}',
                    b'{"schemaVersion":NaN}', b'{"schemaVersion":Infinity}',
                    b'{"schemaVersion":-Infinity}', b'{"schemaVersion":1e400}']
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'document.json'
            for payload in payloads:
                path.write_bytes(payload)
                with self.subTest(payload=payload), self.assertRaises(ValidationError):
                    load_document(path)
            text = json.dumps(self.document).replace('"title":', '"title":"duplicate","title":', 1)
            path.write_text(text, encoding='utf-8')
            with self.assertRaises(ValidationError):
                load_document(path)

    def test_unpaired_surrogate_in_text_is_rejected(self):
        self.document['blocks'][0]['title'] = '\ud800'
        with self.assertRaises(ValidationError):
            validate_document(self.document)

    def test_oversize_and_deeply_nested_documents_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'document.json'
            for payload in [b' ' * (1024 * 1024 + 1), b'[' * 1500 + b']' * 1500]:
                path.write_bytes(payload)
                with self.subTest(size=len(payload)), self.assertRaises(ValidationError):
                    load_document(path)


if __name__ == '__main__':
    unittest.main()
