"""Tests for the Baseten inference client (all network calls mocked)."""
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from agent.client import BasetenAPIError, BasetenConfigError, chat


def tool_call(call_id='call_1', name='get_fee_quote', arguments='{"amount": "50000"}'):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def completion(text='Hello', calls=None, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text, tool_calls=calls)
        )],
        usage=usage,
    )


class BasetenClientTest(TestCase):
    @override_settings(BASETEN_API_KEY='')
    def test_missing_key_is_a_clear_config_error(self):
        with self.assertRaises(BasetenConfigError):
            chat([{'role': 'user', 'content': 'hi'}])

    @override_settings(BASETEN_API_KEY='test-key')
    def test_plain_reply_is_returned_as_text(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                completion(text='Hello there')
            )
            result = chat([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(result['text'], 'Hello there')
        self.assertEqual(result['tool_calls'], [])

    @override_settings(BASETEN_API_KEY='test-key')
    def test_tool_calls_are_parsed_to_plain_data(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                completion(text='', calls=[tool_call()])
            )
            result = chat(
                [{'role': 'user', 'content': 'fee?'}],
                tools=[{'type': 'function', 'function': {'name': 'get_fee_quote'}}],
            )
        self.assertEqual(len(result['tool_calls']), 1)
        call = result['tool_calls'][0]
        self.assertEqual(call['name'], 'get_fee_quote')
        self.assertEqual(call['arguments'], {'amount': '50000'})

    @override_settings(BASETEN_API_KEY='test-key')
    def test_unparseable_arguments_are_an_api_error(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                completion(calls=[tool_call(arguments='{not json')])
            )
            with self.assertRaises(BasetenAPIError):
                chat([{'role': 'user', 'content': 'hi'}])

    @override_settings(BASETEN_API_KEY='test-key')
    def test_timeout_maps_to_api_error(self):
        from openai import APITimeoutError
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.side_effect = (
                APITimeoutError(request=None)
            )
            with self.assertRaises(BasetenAPIError):
                chat([{'role': 'user', 'content': 'hi'}])

    @override_settings(BASETEN_API_KEY='test-key')
    def test_empty_choices_are_an_api_error(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                SimpleNamespace(choices=[])
            )
            with self.assertRaises(BasetenAPIError):
                chat([{'role': 'user', 'content': 'hi'}])

    @override_settings(BASETEN_API_KEY='test-key')
    def test_usage_is_logged_not_returned_raw(self):
        with patch('agent.client.build_client') as mock_build:
            mock_build.return_value.chat.completions.create.return_value = (
                completion(usage=SimpleNamespace(
                    prompt_tokens=10, completion_tokens=5
                ))
            )
            result = chat([{'role': 'user', 'content': 'hi'}])
        self.assertEqual(result['usage']['prompt_tokens'], 10)
        self.assertEqual(result['usage']['completion_tokens'], 5)
