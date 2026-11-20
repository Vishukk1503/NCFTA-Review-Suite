"""Source revocation coverage; synthetic data and mocked HTTP only."""
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import suite


class DeletionFetchTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.run = Path(tmp.name)
        self.cfg = {'url': 'https://example.invalid', 'transport': 'requests'}
        suite.save(self.run / 'manifest.json', {
            'filters': {'from': '2026-11-09', 'to': '2026-11-09', 'groups': [6]}})
        self.event = {'uuid': 'synthetic-event', 'date': '2026-11-09',
                      'sharing_group_id': '6', 'Attribute': []}

    def client(self, event):
        client = Mock()
        client.search.side_effect = [{'response': [{'Event': event}]}, {'response': []}]
        return client

    def fetch(self, client):
        return suite.fetch(self.run, self.cfg, Mock(),
                           lambda: contextlib.nullcontext(client))

    def test_both_states_requested_and_retained_in_raw_and_records(self):
        self.event['Attribute'] = [
            {'uuid': 'active', 'type': 'other', 'value': 'Synthetic', 'deleted': False},
            {'uuid': 'revoked', 'type': 'other', 'value': 'Synthetic', 'deleted': True},
        ]
        client = self.client(self.event)
        rows = self.fetch(client)
        self.assertEqual([r['deleted'] for r in rows], [False, True])
        for call in client.search.call_args_list:
            self.assertEqual(call.kwargs['deleted'], [0, 1])
            self.assertEqual(call.kwargs['sharinggroup'], [6])
            self.assertEqual(call.kwargs['limit'], 10)
            self.assertFalse(call.kwargs['with_attachments'])
        self.assertEqual(suite.read(self.run / 'events.json')['response'][0]['Event'], self.event)
        self.assertEqual(suite.read(self.run / 'manifest.json')['misp_deleted_filter'], [0, 1])

    def test_requests_transport_serializes_filter_without_disabling_tls(self):
        with patch('tls_support.configure_tls', return_value=(True, 'test')), \
                patch('requests.Session') as session:
            session.return_value.post.return_value.status_code = 200
            session.return_value.post.return_value.json.return_value = {'response': []}
            with suite.HTTPClient(self.cfg['url'], 'synthetic-key') as client:
                client.search(sharinggroup=[6], date_from='2026-11-09',
                              date_to='2026-11-09', page=1, limit=10, deleted=[0, 1])
            args = session.return_value.post.call_args.kwargs
            self.assertEqual(args['json']['deleted'], [0, 1])
            self.assertIs(args['verify'], True)
            self.assertFalse(args['allow_redirects'])

    def test_object_revocation_is_inherited_but_proposal_is_not(self):
        self.event['Attribute'] = [{'uuid': 'a', 'value': 'Synthetic', 'deleted': False,
                                   'ShadowAttribute': [{'proposal_to_delete': True}]}]
        self.event['Object'] = [
            {'uuid': 'o', 'deleted': True, 'Attribute': [
                {'uuid': 'b', 'value': 'Synthetic', 'deleted': False}]},
            {'uuid': 'p', 'deleted': False, 'Attribute': [
                {'uuid': 'c', 'value': 'Synthetic', 'deleted': False}]},
        ]
        rows = self.fetch(self.client(self.event))
        self.assertEqual([r['deleted'] for r in rows], [False, True, False])

    def test_legacy_partial_run_stops_before_network_and_preserves_files(self):
        page = self.run / 'raw/group-6-page-000001.json'
        suite.save(page, {'response': []})
        original = page.read_bytes()
        manifest = (self.run / 'manifest.json').read_bytes()
        client = Mock()
        with self.assertRaisesRegex(suite.SuiteError, 'Start a new run'):
            self.fetch(client)
        client.search.assert_not_called()
        self.assertEqual(page.read_bytes(), original)
        self.assertEqual((self.run / 'manifest.json').read_bytes(), manifest)

    def test_matching_policy_can_resume_and_rechecks_saved_pages(self):
        m = suite.read(self.run / 'manifest.json')
        m['misp_deleted_filter'] = [0, 1]
        suite.save(self.run / 'manifest.json', m)
        suite.save(self.run / 'raw/group-6-page-000001.json',
                   {'response': [{'Event': self.event}]})
        self.fetch(self.client(self.event))
        self.assertTrue(suite.read(self.run / 'manifest.json')['fetch_complete'])

    def test_changed_page_under_same_policy_is_rejected(self):
        m = suite.read(self.run / 'manifest.json')
        m['misp_deleted_filter'] = [0, 1]
        suite.save(self.run / 'manifest.json', m)
        suite.save(self.run / 'raw/group-6-page-000001.json', {'response': []})
        with self.assertRaisesRegex(suite.SuiteError, 'Saved page changed'):
            self.fetch(self.client(self.event))

    def test_empty_result_is_success_and_does_not_create_deletions(self):
        client = Mock()
        client.search.return_value = {'response': []}
        self.assertEqual(self.fetch(client), [])
        manifest = suite.read(self.run / 'manifest.json')
        self.assertTrue(manifest['fetch_complete'])
        self.assertEqual(manifest['records'], 0)


if __name__ == '__main__':
    unittest.main()
