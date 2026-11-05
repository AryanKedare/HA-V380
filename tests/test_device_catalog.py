"""Catalog refresh/ownership regressions with synthetic cloud replies only."""
import asyncio
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

PACKAGE = '_v380_catalog_tests'
package = types.ModuleType(PACKAGE)
package.__path__ = [str(Path(__file__).resolve().parents[1] / 'custom_components/v380_cloud')]
sys.modules[PACKAGE] = package
account = importlib.import_module(PACKAGE + '.account')


def reply(ids=(), shares=(), *, result=0):
    return {'result': result, 'error_code': 0,
            'data': [{'device_id': uid, 'nickname': f'fixture-{uid}'} for uid in ids],
            'data_share': [{'device_id': uid, 'str': ''} for uid in shares]}


def login():
    return {'result': 0, 'error_code': 0, 'data': {'access_token': 'fresh-fixture', 'user_id': 42}}


class CatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = account.AccountClient(None, 'fixture-account', 'fixture-password')
        self.client.access_token, self.client.account_id, self.client.user_id = 'old-fixture', '42', 42

    async def asyncTearDown(self):
        await self.client.async_close()

    async def seed(self, ids=(7,), shares=()):
        with patch.object(self.client, '_request_json', return_value=reply(ids, shares)):
            await self.client.async_devices()

    async def test_partial_result_updates_but_cannot_delete_known_cameras(self):
        await self.seed((7, 8))
        partial = reply((7, 9), result=10005)
        partial['data'][0]['nickname'] = 'updated'
        with patch.object(self.client, '_request_json', return_value=partial) as request:
            await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7', '8', '9'})
        self.assertEqual(self.client.devices['7'].name, 'updated')
        self.assertFalse(self.client.device_list_complete)
        self.assertEqual(self.client.last_device_list_result, 10005)
        self.assertEqual(self.client.partial_device_list_responses, 1)
        self.assertEqual(request.await_count, 1)

    async def test_partial_result_without_sections_retains_existing_catalog(self):
        await self.seed()
        with patch.object(self.client, '_request_json', return_value={'result': 10007, 'error_code': 0}):
            await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7'})
        self.assertEqual(self.client.empty_catalog_recoveries, 0)

    async def test_missing_owned_section_does_not_remove_owned_camera(self):
        await self.seed()
        value = reply(shares=(8,)); value.pop('data')
        with patch.object(self.client, '_request_json', return_value=value):
            await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7', '8'})

    async def test_missing_shared_section_does_not_remove_shared_camera(self):
        await self.seed(shares=(8,))
        value = reply((7,)); value['data_share'] = None
        with patch.object(self.client, '_request_json', return_value=value):
            await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7', '8'})
        self.assertTrue(self.client.devices['8'].metadata['shared'])

    async def test_missing_all_sections_is_not_a_successful_empty_catalog(self):
        await self.seed()
        with patch.object(self.client, '_request_json', return_value={'result': 0, 'error_code': 0}):
            with self.assertRaises(account.CloudError): await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7'})
        self.assertFalse(self.client.device_list_complete)

    async def test_invalid_section_type_does_not_modify_existing_catalog(self):
        await self.seed()
        value = reply(); value['data'] = {'unexpected': True}
        with patch.object(self.client, '_request_json', return_value=value):
            with self.assertRaises(account.CloudError): await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7'})

    async def test_malformed_shared_row_cannot_confirm_a_removed_share(self):
        await self.seed(shares=(8,))
        value = reply((7,)); value['data_share'] = [{'device_id': 8, 'str': '!invalid!'}]
        with patch.object(self.client, '_request_json', return_value=value):
            await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7', '8'})
        self.assertFalse(self.client.device_list_complete)

    async def test_empty_catalog_refreshes_saved_session_and_recovers_camera(self):
        await self.seed()
        with patch.object(self.client, '_request_json', side_effect=[reply(), login(), reply((7,))]) as request:
            await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7'})
        self.assertEqual([call.args[0].rsplit('/', 1)[-1] for call in request.await_args_list], ['list', 'login', 'list'])
        self.assertEqual(self.client.device_removal_checks, 1)
        self.assertEqual(self.client.empty_catalog_recoveries, 1)
        self.assertTrue(self.client.device_list_complete)

    async def test_confirmed_empty_catalog_removes_devices_without_login_loop(self):
        await self.seed()
        with patch.object(self.client, '_request_json', side_effect=[reply(), login(), reply()]):
            self.assertEqual(await self.client.async_devices(), [])
        self.assertTrue(self.client.device_list_complete)
        with patch.object(self.client, '_request_json', return_value=reply()) as request:
            self.assertEqual(await self.client.async_devices(), [])
        self.assertEqual(request.await_count, 1)
        self.assertEqual(self.client.empty_catalog_recoveries, 1)

    async def test_confirmed_subset_removal_does_not_force_account_login(self):
        await self.seed((7, 8))
        with patch.object(self.client, '_request_json', side_effect=[reply((7,)), reply((7,))]) as request:
            await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7'})
        self.assertTrue(all(call.args[0].endswith('device/list') for call in request.await_args_list))

    async def test_transient_subset_omission_is_recovered_without_erasing_camera(self):
        await self.seed((7, 8))
        with patch.object(self.client, '_request_json', side_effect=[reply((7,)), reply((7, 8))]):
            await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7', '8'})

    async def test_different_omissions_cannot_confirm_each_other(self):
        await self.seed((7, 8))
        with patch.object(self.client, '_request_json', side_effect=[reply((7,)), reply((8,))]):
            await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7', '8'})
        self.assertFalse(self.client.device_list_complete)

    async def test_failed_confirmation_keeps_last_committed_catalog(self):
        await self.seed((7, 8))
        with patch.object(self.client, '_request_json', side_effect=[reply((7,)), account.CloudError('temporary')]):
            with self.assertRaises(account.CloudError): await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7', '8'})

    async def test_failed_saved_login_keeps_catalog_and_propagates_real_auth_error(self):
        await self.seed()
        with patch.object(self.client, '_request_json', side_effect=[reply(), {'result': -1, 'error_code': 21006}]):
            with self.assertRaises(account.AuthError): await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7'})
        self.assertEqual(self.client.access_token, '')

    async def test_concurrent_refreshes_share_one_committed_snapshot(self):
        await self.seed()
        async def response(*args):
            await asyncio.sleep(0)
            return reply(result=10005)
        with patch.object(self.client, '_request_json', side_effect=response) as request:
            results = await asyncio.gather(*(self.client.async_devices() for _ in range(12)))
        self.assertEqual(request.await_count, 1)
        self.assertTrue(all([device.device_id for device in result] == ['7'] for result in results))

    async def test_unload_during_confirmation_cannot_restore_cameras(self):
        await self.seed((7, 8))
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def response(*args):
            nonlocal count
            count += 1
            if count == 2:
                entered.set(); await release.wait()
            return reply((7,))
        with patch.object(self.client, '_request_json', side_effect=response):
            task = asyncio.create_task(self.client.async_devices())
            await entered.wait()
            await self.client.async_close(); release.set()
            with self.assertRaises(account.CloudError): await task
        self.assertFalse(self.client.devices)

    async def test_owned_section_can_confirm_removal_when_shared_section_is_absent(self):
        await self.seed()
        empty = reply(); empty.pop('data_share')
        with patch.object(self.client, '_request_json', side_effect=[empty, login(), empty]):
            self.assertEqual(await self.client.async_devices(), [])

    async def test_token_expiry_during_confirmation_retries_without_dropping_camera(self):
        await self.seed((7, 8))
        with patch.object(self.client, '_request_json', side_effect=[reply((7,)),
                {'result': -1, 'error_code': 21011}, login(), reply((7, 8))]):
            await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7', '8'})

    async def test_initial_partial_reply_without_a_catalog_is_not_false_discovery(self):
        with patch.object(self.client, '_request_json', return_value={'result': 10007, 'error_code': 0}):
            with self.assertRaises(account.CloudError): await self.client.async_devices()
        self.assertFalse(self.client.devices)

    async def test_boolean_owned_id_is_rejected_without_mutating_cache(self):
        await self.seed()
        value = reply((True,))
        with patch.object(self.client, '_request_json', return_value=value):
            with self.assertRaises(account.CloudError): await self.client.async_devices()
        self.assertEqual(set(self.client.devices), {'7'})
